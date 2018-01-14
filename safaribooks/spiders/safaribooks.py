import codecs
import json
import os
import re
import shutil
import tempfile
from functools import partial

import scrapy
# from scrapy.shell import inspect_response
from jinja2 import Template
from bs4 import BeautifulSoup

from .. import utils

PAGE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
    <head>
        <title></title>
        <style>
        p.pre {
            font-family: monospace;
            white-space: pre;
        }
        </style>
    </head>
    {{body}}
</html>"""


# def url_base(u):
#   # Actually I can use urlparse, but don't want to fall into the trap of py2 py3
#   # Does this code actually support py3?
#   try:
#     idx = u.rindex('/')
#   except:
#     idx = len(u)
#   return u[:idx]


class SafariBooksSpider(scrapy.spiders.Spider):
    toc_url = 'https://www.safaribooksonline.com/nest/epub/toc/?book_id='
    name = 'SafariBooks'
    # allowed_domains = []
    start_urls = ['https://www.safaribooksonline.com/']
    host = 'https://www.safaribooksonline.com/'

    def __init__(
        self,
        user,
        password,
        bookid,
        output_directory=None,
    ):
        self.user = user
        self.password = password
        self.bookid = str(bookid)
        self.output_directory = utils.mkdirp(
            output_directory or tempfile.mkdtemp()
        )
        self.book_name = ''
        self.epub_path = ''
        self.info = {}
        self._stage_toc = False
        self.tmpdir = tempfile.mkdtemp()
        self._initialize_tempdir()

    def _initialize_tempdir(self):
        self.logger.info(
            'Using `{0}` as temporary directory'.format(self.tmpdir)
        )

        # `copytree` doesn't like when the target directory already exists.
        os.rmdir(self.tmpdir)

        shutil.copytree(utils.pkg_path('data/'), self.tmpdir)

    def parse(self, response):
        return scrapy.FormRequest.from_response(
            response,
            formdata={'email': self.user, 'password1': self.password},
            callback=self.after_login
        )

    def after_login(self, response):
        # Loose role to decide if user signed in successfully.
        if '/login' in response.url:
            self.logger.error('Failed login')
            return
        yield scrapy.Request(
            self.toc_url + self.bookid,
            callback=self.parse_toc,
        )

    def parse_cover_img(self, name, response):
        # inspect_response(response, self)
        cover_img_path = os.path.join(self.tmpdir, 'OEBPS', 'cover-image.jpg')
        with open(cover_img_path, 'w') as fh:
            fh.write(response.body)

    def parse_content_img(self, img, response):
        img_path = os.path.join(os.path.join(self.tmpdir, 'OEBPS'), img)

        img_dir = os.path.dirname(img_path)
        if not os.path.exists(img_dir):
            os.makedirs(img_dir)

        with open(img_path, 'wb') as fh:
            fh.write(response.body)

    def parse_page_json(self, title, bookid, response):
        page_json = json.loads(response.body)
        yield scrapy.Request(
            page_json['content'],
            callback=partial(
                self.parse_page,
                title,
                bookid,
                page_json['full_path'],
                page_json['images'],
            ),
        )

    def parse_page(self, title, bookid, path, images, response):
        template = Template(PAGE_TEMPLATE)

        # path might have nested directory
        dirs_to_make = os.path.join(
            self.tmpdir,
            'OEBPS',
            os.path.dirname(path),
        )
        if not os.path.exists(dirs_to_make):
            os.makedirs(dirs_to_make)

        oebps_body_path = os.path.join(self.tmpdir, 'OEBPS', path)
        with codecs.open(oebps_body_path, 'wb', 'utf-8') as fh:
            body = BeautifulSoup(response.body, 'lxml').find('body')
            fh.write(template.render(body=body))

        for img in images:
            if not img:
                continue

            # fix for books which are one level down
            img = img.replace('../', '')

            yield scrapy.Request(
                '/'.join((self.host, 'library/view', title, bookid, img)),
                callback=partial(self.parse_content_img, img),
            )

    def parse_toc(self, response):
        try:
            toc = json.loads(response.body)
        except Exception:
            self.logger.error(
                'Failed evaluating toc body: {0}'.format(response.body),
            )
            return

        self._stage_toc = True

        self.book_name = toc['title_safe']
        self.book_title = re.sub(r'["%*/:<>?\\|~\s]', r'_', toc['title'])  # to be used for filename

        cover_path, = re.match(
            r'<img src="(.*?)" alt.+',
            toc['thumbnail_tag'],
        ).groups()

        yield scrapy.Request(
            self.host + cover_path,
            callback=partial(self.parse_cover_img, 'cover-image'),
        )

        for item in toc['items']:
            yield scrapy.Request(
                self.host + item['url'],
                callback=partial(
                    self.parse_page_json,
                    toc['title_safe'],
                    toc['book_id'],
                ),
            )

        content_path = os.path.join(self.tmpdir, 'OEBPS', 'content.opf')
        with open(content_path) as fh:
            template = Template(fh.read())
        with codecs.open(content_path, 'wb', 'utf-8') as fh:
            fh.write(template.render(info=toc))

        toc_path = os.path.join(self.tmpdir, 'OEBPS', 'toc.ncx')
        with open(toc_path) as fh:
            template = Template(fh.read())
        with codecs.open(toc_path, 'wb', 'utf-8') as fh:
            fh.write(template.render(info=toc))

    def closed(self, reason):
        if self._stage_toc is False:
            self.logger.info(
                'Did not even got toc, ignore generated file operation.'
            )
            return

        zip_path = shutil.make_archive(self.book_name, 'zip', self.tmpdir)
        self.logger.info('Made archive {0}'.format(zip_path))

        self.epub_path = os.path.join(
            self.output_directory,
            '{0}-{1}.epub'.format(self.book_title, self.bookid),
        )
        self.logger.info('Moving {0} to {1}'.format(zip_path, self.epub_path))
        shutil.move(zip_path, self.epub_path)
