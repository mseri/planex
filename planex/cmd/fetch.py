"""
planex-fetch: Download sources referred to by a spec file
"""

import argparse
import logging
import os
import shutil
import sys
import urlparse

import argcomplete
import pkg_resources
import pycurl

from planex.link import Link
from planex.cmd.args import common_base_parser, rpm_define_parser
from planex.util import run
from planex.util import setup_logging
from planex.util import setup_sigint_handler
import planex.spec


# This should include all of the extensions in the Makefile.rules for fetch
SUPPORTED_EXT_TO_MIME = {
    '.tar': 'application/x-tar',
    '.gz': 'application/x-gzip',
    '.tgz': 'application/x-gzip',
    '.txz': 'application/x-gzip',
    '.bz2': 'application/x-bzip2',
    '.tbz': 'application/x-bzip2',
    '.zip': 'application/zip',
    '.pdf': 'application/pdf',
    '.patch': 'text/x-diff'
}

SUPPORTED_URL_SCHEMES = ["http", "https", "file", "ftp"]


def curl_get(url_string, out_file):
    """
    Fetch the contents of url and store to file represented by out_file
    """
    curl = pycurl.Curl()

    # General options
    useragent = "planex-fetch/%s" % pkg_resources.require("planex")[0].version
    curl.setopt(pycurl.USERAGENT, useragent)
    curl.setopt(pycurl.FOLLOWLOCATION, True)
    curl.setopt(pycurl.MAXREDIRS, 5)
    curl.setopt(pycurl.CONNECTTIMEOUT, 30)
    curl.setopt(pycurl.TIMEOUT, 300)
    curl.setopt(pycurl.FAILONERROR, True)

    # Cribbed from /usr/lib64/python2.6/site-packages/curl/__init__.py
    curl.setopt(pycurl.SSL_VERIFYHOST, 2)
    curl.setopt(pycurl.COOKIEFILE, "/dev/null")
    curl.setopt(pycurl.NETRC, 1)
    # If we use threads, we should also set NOSIGNAL and ignore SIGPIPE

    # Set URL to fetch and file to which to write the response
    curl.setopt(pycurl.URL, str(url_string))
    curl.setopt(pycurl.WRITEDATA, out_file)

    try:
        curl.perform()
    finally:
        curl.close()


def best_effort_file_verify(path):
    """
    Given a path, check if the file at that path has a sensible format.
    If the file has an extension then it checks that the mime-type of this file
    matches that of the file extension as defined by the IANA:
        http://www.iana.org/assignments/media-types/media-types.xhtml
    """
    _, ext = os.path.splitext(path)
    if ext and ext in SUPPORTED_EXT_TO_MIME:
        # output of `file` is of form: "<path>: <mime-type>"
        cmd = ["file", "--mime-type", path]
        stdout = run(cmd, check=False)['stdout'].strip()
        _, _, mime_type = stdout.partition(': ')

        if SUPPORTED_EXT_TO_MIME[ext] != mime_type:
            sys.exit("%s: Fetched file format looks incorrect: %s: %s" %
                     (sys.argv[0], path, mime_type))


def parse_args_or_exit(argv=None):
    """
    Parse command line options
    """
    parser = argparse.ArgumentParser(description='Download package sources',
                                     parents=[common_base_parser(),
                                              rpm_define_parser()])
    parser.add_argument('spec_or_link', help='RPM Spec or link file')
    parser.add_argument("source", metavar="SOURCE",
                        help="Source file to fetch")
    parser.add_argument('--retries', '-r',
                        help='Number of times to retry a failed download',
                        type=int, default=5)
    parser.add_argument('--no-package-name-check', dest="check_package_names",
                        action="store_false", default=True,
                        help="Don't check that package name matches spec "
                        "file name")
    argcomplete.autocomplete(parser)
    return parser.parse_args(argv)


def fetch_http(url, filename, retries):
    """
    Download the file at url and store it as filename
    """

    while True:
        retries -= 1
        try:
            url_string = urlparse.urlunparse(url)
            logging.debug("Fetching %s to %s", url_string, filename)

            tmp_filename = filename + "~"
            with open(tmp_filename, "wb") as tmp_file:
                curl_get(url_string, tmp_file)
                best_effort_file_verify(tmp_filename)
                shutil.move(tmp_filename, filename)
                # Write an origin file for tracking.
                with open('{0}.origin'.format(filename), 'w') as origin_file:
                    origin_file.write('{0}\n'.format(url_string))
                return

        except pycurl.error as exn:
            logging.debug(exn.args[1])
            if not retries > 0:
                raise


def fetch_url(url, source, retries):
    """Fetch from specified URL"""
    try:
        fetch_http(url, source, retries)

    except pycurl.error as exn:
        # Curl download failed
        sys.exit("%s: Failed to fetch %s: %s" %
                 (sys.argv[0], urlparse.urlunparse(url), exn.args[1]))

    except IOError as exn:
        # IO error saving source file
        sys.exit("%s: %s: %s" %
                 (sys.argv[0], exn.strerror, exn.filename))


def fetch_source(args):
    """
    Download requested source using URL from spec file.
    """

    spec = planex.spec.Spec(args.spec_or_link,
                            check_package_name=args.check_package_names,
                            defines=args.define)

    try:
        path, url = spec.source(args.source)
    except KeyError as exn:
        sys.exit("%s: No source corresponding to %s" % (sys.argv[0], exn))

    url = urlparse.urlparse(url)
    if url.scheme in SUPPORTED_URL_SCHEMES:
        fetch_url(url, path, args.retries + 1)

    elif url.scheme == '' and os.path.dirname(url.path) == '':
        if not os.path.exists(path):
            sys.exit("%s: Source not found: %s" % (sys.argv[0], path))

        # Source file is pre-populated in the SOURCES directory (part of
        # the repository - probably a patch or local include).   Update
        # its timestamp to placate make, but don't try to download it.
        logging.debug("Refreshing timestamp for local source %s", path)
        os.utime(path, None)

    else:
        sys.exit("%s: Unsupported url scheme %s" %
                 (sys.argv[0], url.scheme))


def fetch_via_link(args):
    """
    Parse link file and download patch tarball.
    """
    link = Link(args.spec_or_link)

    if link.schema_version == 1:
        url = urlparse.urlparse(str(link.url))
        fetch_url(url, args.source, args.retries + 1)
    else:
        target, _ = os.path.splitext(os.path.basename(args.source))
        patch_urls = link.patch_sources
        if target in patch_urls:
            patch = patch_urls.get(target)
            url = urlparse.urlparse(patch['URL'])
            fetch_url(url, args.source, args.retries + 1)

        patchqueues = link.patchqueue_sources
        if target in patchqueues:
            patchqueue = patchqueues.get(target)
            url = urlparse.urlparse(patchqueue['URL'])
            fetch_url(url, args.source, args.retries + 1)


def main(argv=None):
    """
    Main function.  Fetch sources directly or via a link file.
    """
    setup_sigint_handler()
    args = parse_args_or_exit(argv)
    setup_logging(args)

    if args.spec_or_link.endswith('.spec'):
        fetch_source(args)
    elif args.spec_or_link.endswith('.lnk'):
        fetch_via_link(args)
    else:
        sys.exit("%s: Unsupported file type: %s" % (sys.argv[0],
                                                    args.spec_or_link))
