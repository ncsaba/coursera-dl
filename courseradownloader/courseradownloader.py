import re
import urllib
import urllib2
import argparse
import json
import os
import getpass
import netrc
import mechanize
import cookielib
import platform
import shutil
import sys
import tarfile
import math
from bs4 import BeautifulSoup
import tempfile
from os import path
from util import *
import _version

MAX_PATH_LENGTH_WINDOWS = 260
MAX_PATH_LENGTH_LINUX = 4096

class CourseraDownloader(object):
    """
    Class to download content (videos, lecture notes, ...) from coursera.org for
    use offline.

    https://github.com/dgorissen/coursera-dl

    :param username: username
    :param password: password
    :keyword proxy: http proxy, eg: foo.bar.com:1234
    :keyword parser: xml parser
    :keyword ignorefiles: comma separated list of file extensions to skip (e.g., "ppt,srt")
    :keyword includefiles: comma separated list of file extensions to download (e.g., "pdf")
    """
    BASE_URL =    'https://class.coursera.org/%s'
    HOME_URL =    BASE_URL + '/class/index'
    LECTURE_URL = BASE_URL + '/lecture/index'
    QUIZ_URL =    BASE_URL + '/quiz/index'
    AUTH_URL =    BASE_URL + "/auth/auth_redirector?type=login&subtype=normal"
    LOGIN_URL =   "https://accounts.coursera.org/api/v1/login"
    ABOUT_URL =   "https://www.coursera.org/maestro/api/topic/information?topic-id=%s"

    #see http://www.crummy.com/software/BeautifulSoup/bs4/doc/#installing-a-parser
    DEFAULT_PARSER = "html.parser"

    # how long to try to open a URL before timing out
    TIMEOUT=60.0

    HTML_TEMPLATE = '''<!DOCTYPE html>
    <html><head>
        <meta charset="utf-8">
        <title>Course materials</title>
        <style type="text/css">
          body { font-family: Sans-Serif; font-size: 90%%; }
          a { font-size: 90%%; color: #07c; }
          a:visited { color: #07c; }
          h3 { color: #808080; }
          div { color: #404040; margin: 10px; }
        </style>
    </head><body>
    %s
    </body></html>'''


    def __init__(self,username,
                        password,
                        proxy=None,
                        parser=DEFAULT_PARSER,
                        ignorefiles=None,
                        includefiles=None,
                        max_path_part_len=None,
                        gzip_courses=False,
                        wk_filter=None,
                        lang=None):

        self.username = username
        self.password = password
        self.parser = parser

        self.ignorefiles =  self.parseFileExtensions(ignorefiles)
        self.includefiles = self.parseFileExtensions(includefiles)

        self.browser = None
        self.proxy = proxy
        self.max_path_part_len = max_path_part_len
        self.gzip_courses = gzip_courses
        self.lang = lang

        self.html = ""

        try:
            self.wk_filter = map(int,wk_filter.split(",")) if wk_filter else None
        except Exception as e:
            print "Invalid week filter, should be a comma separated list of integers", e
            exit()

    @staticmethod
    def parseFileExtensions(extensionsStr):
        """
        Split strings with file extensions ("ignorefiles" and "includefiles" arguments) on commas,
        strip, remove prefixing dot if there is one, and filter out empty tokens.
        """
        return [x.strip()[1:] if x[0]=='.' else x.strip()
                for x in extensionsStr.split(',') if len(x)]

    def login(self,className):
        """
        Login into coursera and obtain the necessary session cookies.
        """
        hn,fn = tempfile.mkstemp()
        cookies = cookielib.LWPCookieJar()
        handlers = [
            urllib2.HTTPHandler(),
            urllib2.HTTPSHandler(),
            urllib2.HTTPCookieProcessor(cookies)
        ]

        # prepend a proxy handler if defined
        if(self.proxy):
            proxy = urllib2.ProxyHandler({'http': self.proxy})
            handlers = [proxy] + handlers

        opener = urllib2.build_opener(*handlers)

        url = self.lecture_url_from_name(className)
        req = urllib2.Request(url)

        try:
            res = opener.open(req)
        except urllib2.HTTPError as e:
            if e.code == 404:
                raise Exception("Unknown class %s" % className)

        # get the csrf token
        csrfcookie = [c for c in cookies if c.name == "csrf_token"]
        if not csrfcookie: raise Exception("Failed to find csrf cookie")
        csrftoken = csrfcookie[0].value
        opener.close()

        # call the authenticator url:
        cj = cookielib.MozillaCookieJar(fn)
        opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj),
                                    urllib2.HTTPHandler(),
                                    urllib2.HTTPSHandler())

        opener.addheaders.append(('Cookie', 'csrftoken=%s' % csrftoken))
        opener.addheaders.append(('Referer', 'https://www.coursera.org'))
        opener.addheaders.append(('X-CSRFToken', csrftoken))
        req = urllib2.Request(self.LOGIN_URL)

        data = urllib.urlencode({'email': self.username,'password': self.password,'webrequest': 'true'})
        req.add_data(data)

        try:
            opener.open(req)
            # timeout=self.TIMEOUT should be handled
        except urllib2.HTTPError as e:
            if e.code == 401:
                raise Exception("Invalid username or password")

        # check if we managed to login
        sessionid = [c.name for c in cj if c.name == "CAUTH"]
        if not sessionid:
            raise Exception("Failed to authenticate as %s" % self.username)

        # all should be ok now, mechanize can handle the rest if we give it the
        # cookies
        br = mechanize.Browser()
        #br.set_debug_http(True)
        #br.set_debug_responses(False)
        #br.set_debug_redirects(True)
        br.set_handle_robots(False)
        br.set_cookiejar(cj)

        if self.proxy:
            br.set_proxies({"http":self.proxy})

        self.browser = br

        # also use this cookiejar for other mechanize operations (e.g., urlopen)
        opener = mechanize.build_opener(mechanize.HTTPCookieProcessor(cj))
        mechanize.install_opener(opener)

    def course_name_from_url(self,course_url):
        """Given the course URL, return the name, e.g., algo2012-p2"""
        return course_url.split('/')[3]

    def lecture_url_from_name(self,course_name):
        """Given the name of a course, return the video lecture url"""
        return self.LECTURE_URL % course_name

    def trim_path_part(self,s):
        mppl = self.max_path_part_len
        if mppl and len(s) > mppl:
            return s[:mppl]
        else:
            return s

    def get_downloadable_content(self,course_url):
        """
        Given the video lecture URL of the course, return a list of all
        downloadable resources.
        """

        cname = self.course_name_from_url(course_url)

        print "* Collecting downloadable content from " + course_url

        # get the course name, and redirect to the course lecture page
        vidpage = self.browser.open(course_url,timeout=self.TIMEOUT)

        # extract the weekly classes
        soup = BeautifulSoup(vidpage,self.parser)

        # extract the weekly classes
        weeks = soup.findAll("div", { "class" : "course-item-list-header" })

        weeklyTopics = []

        # for each weekly class
        for week in weeks:
            # title of this weeks' classes
            h3 = week.findNext('h3')
            weekTopic = sanitise_filename(h3.text)
            weekTopic = self.trim_path_part(weekTopic)

            # get all the classes for the week
            ul = week.next_sibling
            lis = ul.findAll('li')
            weekClasses = []

            # for each class (= lecture)
            classNames = []
            for li in lis:
                # the name of this class
                className = li.a.find(text=True).strip()

                # Many class names have the following format:
                #   "Something really cool (12:34)"
                # If the class name has this format, replace the colon in the
                # time with a hyphen.
                if re.match(".+\(\d?\d:\d\d\)$",className):
                    head,sep,tail = className.rpartition(":")
                    className = head  + "-" + tail

                className = sanitise_filename(className)
                className = self.trim_path_part(className)

                # collect all the resources for this class (ppt, pdf, mov, ..)
                classResources = li.find('div', {'class':'course-lecture-item-resource'})
                hrefs = classResources.findAll('a')
                resourceLinks = []

                for a in hrefs:
                    # get the hyperlink itself
                    h = clean_url(a.get('href'))
                    if not h: continue

                    # Sometimes the raw, uncompresed source videos are available as
                    # well. Don't download them as they are huge and available in
                    # compressed form anyway.
                    if h.find('source_videos') > 0:
                        print "   - will skip raw source video " + h
                    else:
                        if self.lang and h.find('subtitles') > 0:
                            # Substitutes the matched language with user's one
                            def language(match):
                                num = match.group('num')
                                return 'q={num}_{lang}'.format(num=num, lang=self.lang )

                            h = re.sub('q=(?P<num>[\d]+)_\w+', language, h)

                        # Dont set a filename here, that will be inferred from the week
                        # titles
                        resourceLinks.append( (h,None) )

                # check if the video is included in the resources, if not, try
                # do download it directly
                hasvid = [x for x,_ in resourceLinks if x.find('.mp4') > 0]
                if not hasvid:
                    ll = li.find('a',{'class':'lecture-link'})
                    lurl = clean_url(ll['data-modal-iframe'])

                    try:
                        pg = self.browser.open(lurl,timeout=self.TIMEOUT)

                        bb = BeautifulSoup(pg,self.parser)
                        vobj = bb.find('source',type="video/mp4")

                        if not vobj:
                            print " Warning: Failed to find video for %s" %  className
                        else:
                            vurl = clean_url(vobj['src'])
                            # build the matching filename
                            fn = className + ".mp4"
                            resourceLinks.append( (vurl,fn) )

                    except urllib2.HTTPError as e:
                        # sometimes there is a lecture without a video (e.g.,
                        # genes-001) so this can happen.
                        print " Warning: failed to open the direct video link %s: %s" % (lurl,e)

                weekClasses.append( (className,resourceLinks) )

            weeklyTopics.append( (weekTopic, weekClasses) )

        return weeklyTopics

    def get_headers(self,url):
        """
        Get the headers
        """
        r = self.browser.open(url,timeout=self.TIMEOUT)
        return r.info()

    def download(self, url, target_dir=".", target_fname=None, class_dir=None):
        """
        Download the url to the given filename
        """

        # get the headers
        headers = self.get_headers(url)

        # get the content length (if present)
        clen = int(headers.get('Content-Length',-1))

        # build the absolute path we are going to write to
        fname = target_fname or filename_from_header(headers) or filename_from_url(url)

        # split off the extension
        basename, ext = path.splitext(fname)

        # ensure it respects mppl
        fname = self.trim_path_part(basename) + ext

        # check if we should skip it (remember to remove the leading .)
        if ext and ext[1:] in self.ignorefiles:
            print '    - skipping "%s" (extension ignored)' % fname
            return

        # if downloading class resource (as opposed to lecture/syllabus pages), and '-i' arg specified
        # then skip other file extensions (and files with no extensions)
        if (class_dir and self.includefiles and not (ext and ext[1:] in self.includefiles)):
            print '    - skipping "%s" (extension not included)' % fname
            return

        filepath = trim_path(path.join(target_dir, fname), get_max_path_length()-1, 1)

        if (class_dir): self.html += '<a href="%s">%s</a> \n' % (path.join(class_dir, fname), ext[1:])

        dl = True
        if path.exists(filepath):
            if clen > 0:
                fs = path.getsize(filepath)
                delta = math.fabs(clen - fs)

                # there are cases when a file was not completely downloaded or
                # something went wront that meant the file on disk is
                # unreadable. The file on disk my be smaller or larger (!) than
                # the reported content length in those cases.
                # Hence we overwrite the file if the reported content length is
                # different than what we have already by at least k bytes (arbitrary)

                # TODO this is still not foolproof as the fundamental problem is that the content length cannot be trusted
                # so this really needs to be avoided and replaced by something
                # else, eg., explicitly storing what downloaded correctly
                if delta > 10:
                   print '    - "%s" seems corrupt, downloading again' % fname
                else:
                    print '    - "%s" already exists, skipping' % fname
                    dl = False
            else:
                # missing or invalid content length
                # assume all is ok...
                dl = False
        else:
            # Detect renamed files
            existing, short = find_renamed(filepath, clen)
            if existing:
                print '    - "%s" seems to be a copy of "%s", renaming existing file' % (fname, short)
                os.rename(existing, filepath)
                dl = False

        try:
            if dl:
                self.browser.retrieve(url,filepath,timeout=self.TIMEOUT)
        except Exception as e:
            print "Failed to download url %s to %s: %s" % (url,filepath,e)

    def download_about(self, cname, course_dir):
        """
        Download the 'about' json file
        """
        fn = os.path.join(course_dir, cname + '-about.json')

        # get the base course name (without the -00x suffix)
        base_name = re.split('(-[0-9]+)', cname)[0]

        # get the json
        about_url = self.ABOUT_URL % base_name
        about_json = self.browser.open(about_url,timeout=self.TIMEOUT).read()
        data = json.loads(about_json)

        # pretty print to file
        with open(fn, 'w') as f:
            json_data = json.dumps(data, indent=4, separators=(',', ':'))
            f.write(json_data)

    def download_course(self,cname,dest_dir=".",reverse_sections=False,gzip_courses=False):
        """
        Download all the contents (quizzes, videos, lecture notes, ...)
        of the course to the given destination directory (defaults to .)
        """
        # open the main class page
        self.browser.open(self.AUTH_URL % cname,timeout=self.TIMEOUT)

        # get the lecture url
        course_url = self.lecture_url_from_name(cname)

        weeklyTopics = self.get_downloadable_content(course_url)

        if not weeklyTopics:
            print " Warning: no downloadable content found for %s, did you accept the honour code?" % cname
            return
        else:
            print '* Got all downloadable content for ' + cname

        if reverse_sections:
            weeklyTopics.reverse()
            print "* Weekly modules reversed"

        # where the course will be downloaded to
        course_dir = path.abspath(path.join(dest_dir,cname))

        # ensure the course dir exists
        if not path.exists(course_dir):
            os.makedirs(course_dir)

        print "* " + cname + " will be downloaded to " + course_dir

        # download the standard pages
        print " - Downloading lecture/syllabus pages"
        self.download(self.HOME_URL % cname,target_dir=course_dir,target_fname="index.html")
        self.download(course_url,           target_dir=course_dir,target_fname="lectures.html")
        try:
            self.download_about(cname,course_dir)
        except Exception as e:
            print "Warning: failed to download about file",e


        # now download the actual content (video's, lecture notes, ...)
        for j, (weeklyTopic, weekClasses) in enumerate(weeklyTopics,start=1):


            if self.wk_filter and j not in self.wk_filter:
                print " - skipping %s (idx = %s), as it is not in the week filter" % (weeklyTopic,j)
                continue

            # add a numeric prefix to the week directory name to ensure chronological ordering
            wkdirname = str(j).zfill(2) + " - " + weeklyTopic

            # ensure the week dir exists
            wkdir = path.join(course_dir,wkdirname)
            if not path.exists(wkdir):
                os.makedirs(wkdir)

            print " - " + weeklyTopic
            self.html += "<h3>%s</h3>\n" % weeklyTopic

            for i, (className, classResources) in enumerate(weekClasses,start=1):

                # ensure chronological ordering
                clsdirname = str(i).zfill(2) + " - " + className

                # ensure the class dir exists
                clsdir = path.join(wkdir, clsdirname)

                if not path.exists(clsdir):
                    os.makedirs(clsdir)

                print "  - Downloading resources for " + className

                self.html += "<div>%s<br>\n" % className

                # download each resource
                for classResource,tfname in classResources:
                    try:
                        print '    - Downloading ', classResource, tfname
                        self.download(classResource,target_dir=clsdir,target_fname=tfname,
                            class_dir=path.join(wkdirname, clsdirname))
                    except Exception as e:
                        print "    - failed: ",classResource,e
                self.html += "</div>\n"

        try:
            file = open(path.join(course_dir, 'materials.html'), "w")
            file.write(self.HTML_TEMPLATE % self.html)
            file.close()
        except Exception as e:
            print "  - Writing materials.html failed: ",e 

        if gzip_courses:
            tar_file_name = cname + ".tar.gz"
            print "Compressing and storing as " + tar_file_name
            tar = tarfile.open(os.path.join(dest_dir, tar_file_name),'w:gz')
            tar.add(os.path.join(dest_dir, cname),arcname=cname)
            tar.close()
            print "Compression complete. Cleaning up."
            shutil.rmtree(os.path.join(dest_dir, cname))



def get_max_path_length():
    '''
    Gets the maximum path length supported by the operating system
    '''
    if platform.system() == 'Windows':
        return MAX_PATH_LENGTH_WINDOWS
    else:
        return MAX_PATH_LENGTH_LINUX

def get_netrc_creds():
    """
    Read username/password from the users' netrc file. Returns None if no
    coursera credentials can be found.
    """
    # inspired by https://github.com/jplehmann/coursera

    if platform.system() == 'Windows':
        # where could the netrc file be hiding, try a number of places
        env_vars = ["HOME","HOMEDRIVE", "HOMEPATH","USERPROFILE","SYSTEMDRIVE"]
        env_dirs = [os.environ[e] for e in env_vars if os.environ.get(e,None)]

        # also try the root/cur dirs
        env_dirs += ["C:", ""]

        # possible filenames
        file_names = [".netrc", "_netrc"]

        # all possible paths
        paths = [path.join(dir,fn) for dir in env_dirs for fn in file_names]
    else:
        # on *nix just put None, and the correct default will be used
        paths = [None]

    # try the paths one by one and return the first one that works
    creds = None
    for p in paths:
        try:
            auths = netrc.netrc(p).authenticators('coursera-dl')
            creds = (auths[0], auths[2])
            print "Credentials found in .netrc file"
            break
        except (IOError, TypeError, netrc.NetrcParseError) as e:
            pass

    return creds

def normalize_string(str):
    return ''.join(x for x in str if x not in ' \t-_()"01234567890').lower()

def find_renamed(filename, size):
    fpath, name = path.split(filename)
    name, ext = path.splitext(name)
    name = normalize_string(name)

    if not path.exists(fpath):
        return None, None

    files = os.listdir(fpath)
    if files:
        for f in files:
            fname, fext = path.splitext(f)
            fname = normalize_string(fname)
            if fname == name and fext == ext:
                fullname = os.path.join(fpath, f)
                if path.getsize(fullname) == size:
                    return fullname, f

    return None, None

def main():
    # parse the commandline arguments
    parser = argparse.ArgumentParser(description='Download Coursera.org course videos/docs for offline use.')
    parser.add_argument("-u", dest='username', type=str, help='coursera username (.netrc used if omitted)')
    parser.add_argument("-p", dest='password', type=str, help='coursera password')
    parser.add_argument("-d", dest='dest_dir', type=str, default=".", help='destination directory where everything will be saved')
    parser.add_argument("-n", dest='ignorefiles', type=str, default="", help='comma-separated list of file extensions to skip, e.g., "ppt,srt,pdf"')
    parser.add_argument("-i", dest='includefiles', type=str, default="", help='comma-separated list of file extensions to download, e.g., "pdf,doc"')
    parser.add_argument("-l", dest='lang', type=str, help='language of subtitles')
    parser.add_argument("-q", dest='parser', type=str, default=CourseraDownloader.DEFAULT_PARSER,
                        help="the html parser to use, see http://www.crummy.com/software/BeautifulSoup/bs4/doc/#installing-a-parser")
    parser.add_argument("-x", dest='proxy', type=str, default=None, help="proxy to use, e.g., foo.bar.com:3125")
    parser.add_argument("--reverse-sections", dest='reverse', action="store_true",
                        default=False, help="download and save the sections in reverse order")
    parser.add_argument('course_names', nargs="+", metavar='<course name>',
                        type=str, help='one or more course names from the url (e.g., comnets-2012-001)')
    parser.add_argument("--gz",
                        dest='gzip_courses',action="store_true",default=False,help='Tarball courses for archival storage (folders get deleted)')
    parser.add_argument("-mppl", dest='mppl', type=int, default=120,
                        help='Maximum length of filenames/dirs in a path')
    parser.add_argument("-w", dest='wkfilter', type=str, default=None,
                        help="Comma separted list of sequence/lesson/week numbers to download e.g., 1,3,8")
    args = parser.parse_args()

    # check the parser
    html_parser = args.parser
    if html_parser == "html.parser" and sys.version_info < (2,7,3):
        print " Warning: built-in 'html.parser' may cause problems on Python < 2.7.3"

    print "Coursera-dl v%s (%s)" % (_version.__version__,html_parser)

    # search for login credentials in .netrc file if username hasn't been provided in command-line args
    username, password = args.username, args.password
    if not username:
        creds = get_netrc_creds()
        if not creds:
            raise Exception("No username passed and no .netrc credentials found, unable to login")
        else:
            username, password = creds
    else:
        # prompt the user for his password if not specified
        if not password:
            password = getpass.getpass()

    mppl = args.mppl

    # instantiate the downloader class
    d = CourseraDownloader(
                           username,
                           password,
                           proxy=args.proxy,
                           parser=html_parser,
                           ignorefiles=args.ignorefiles,
                           includefiles=args.includefiles,
                           max_path_part_len=mppl,
                           gzip_courses=args.gzip_courses,
                           wk_filter=args.wkfilter,
                           lang=args.lang,
                          )

    # authenticate, only need to do this once but need a classaname to get hold
    # of the csrf token, so simply pass the first one
    print "Logging in as '%s'..." % username
    d.login(args.course_names[0])

    # download the content
    for i,cn in enumerate(args.course_names,start=1):
        print
        print "Course %s of %s" % (i,len(args.course_names))
        d.download_course(cn,dest_dir=args.dest_dir,reverse_sections=args.reverse,gzip_courses = args.gzip_courses)

if __name__ == '__main__':
    main()
