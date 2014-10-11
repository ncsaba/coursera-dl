import re
import urllib2
from urlparse import urlsplit, urlparse
import unicodedata
from os import path

def filename_from_header(header):
    try:
        cd = header['Content-Disposition']
        pattern = 'attachment; filename="(.*?)"'
        m = re.search(pattern, cd)
        g = m.group(1)
        if ("%" in g): g = urllib2.unquote(g)
        return sanitise_filename(g)
    except Exception:
        return ''

def filename_from_url(url):
    # parse the url into its components
    u = urlsplit(url)

    # split the path into parts and unquote
    parts = [urllib2.unquote(x).strip() for x in u.path.split('/')]

    # take the last component as filename
    fname = parts[-1]

    # if empty, url ended with a trailing slash
    # so join up the hostnam/path  and use that as a filename
    if len(fname) < 1:
        s = u.netloc + u.path[:-1]
        fname = s.replace('/','_')
    else:
        # unquoting could have cuased slashes to appear again
        # split and take the last element if so
        fname = fname.split('/')[-1]

    # add an extension if none
    ext = path.splitext(fname)[1]
    if len(ext) < 1 or len(ext) > 5:
        fname += ".html"

    # remove any illegal chars and return
    return sanitise_filename(fname)

def clean_url(url):
    if not url: return None

    url = url.strip()

    if url and not urlparse(url).scheme:
        url = "http://" + url

    return url

def sanitise_filename(fileName):
    # ensure a clean, valid filename (arg may be both str and unicode)

    # ensure a unicode string, problematic ascii chars will get removed
    if isinstance(fileName,str):
        fn = unicode(fileName,errors='ignore')
    else:
        fn = fileName

    # normalize it
    fn = unicodedata.normalize('NFKD',fn)

    # encode it into ascii, again ignoring problematic chars
    s = fn.encode('ascii','ignore')

    # remove any characters not in the whitelist
    s = re.sub('[^\w\-\(\)\[\]\., ]','',s).strip()

    # ensure it is within a sane maximum
    max = 250

    # split off extension, trim, and re-add the extension
    fn,ext = path.splitext(s)
    s = fn[:max-len(ext)] + ext

    return s

def trim_path(pathname, max_path_len=255, min_len=5):
    """
    Trim file name in given path name to fit max_path_len characters. Only file name is trimmed,
    path names are not affected to avoid creating multiple folders for the same lecture.
    """
    if len(pathname) <= max_path_len:
        return pathname

    fpath, name = path.split(pathname)
    name, ext = path.splitext(name)

    to_cut = len(pathname) - max_path_len
    to_keep = len(name) - to_cut

    if to_keep < min_len:
        print ' Warning: Cannot trim filename "%s" to fit required path length (%d)' % (pathname, max_path_len)
        return pathname

    name = name[:to_keep]
    new_pathname = path.join(fpath, name + ext)
    print ' Trimmed path name "%s" to "%s" to fit required length (%d)' % (pathname, new_pathname, max_path_len)

    return new_pathname
