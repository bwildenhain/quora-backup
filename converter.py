#!/usr/bin/env python3
import argparse
import errno
from html5lib import (HTMLParser, serializer, treebuilders, treewalkers)
import os
import re
import sys
import time
import urllib.error
import urllib.request
from xml.dom.minidom import Node

def log_if_v(msg):
    if args.verbose:
        print('[DEBUG] %s' % msg, file=sys.stderr)

def get_title_node(document):
    for node in document.getElementsByTagName('title'):
        return node
    return None

def get_text_content(node):
    text = ''
    for text_node in node.childNodes:
        if text_node.nodeType == Node.TEXT_NODE:
            text += text_node.data
    return text

# Given origin (timestamp offset by time zone) and string from Quora, e.g.
# "Added 31 Jan", returns a string such as '2015-01-31'.
# Quora's short date strings don't provide enough information to determine the
# exact time, unless it was within the last day, so we won't bother to be any
# more precise.
def parse_quora_date(origin, date_str):
    days_of_week = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    months_of_year = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    m0 = re.match('just now$', date_str)
    m1 = re.match('(\d+)m ago$', date_str)
    m2 = re.match('(\d+)h ago$', date_str)
    m3 = re.match('(' + '|'.join(days_of_week) + ')$', date_str)
    m4 = re.match('(' + '|'.join(months_of_year) + ') (\d+)$', date_str)
    m5 = re.match('(' + '|'.join(months_of_year) + ') (\d+), (\d+)$', date_str)
    m6 = re.match('(\d+)[ap]m$', date_str)
    if not m0 is None or not m6 is None:
        # Using origin for time in am / pm since the time of the day will be discarded anyway
        tm = time.gmtime(origin)
    elif not m1 is None:
        tm = time.gmtime(origin - 60*int(m1.group(1)))
    elif not m2 is None:
        tm = time.gmtime(origin - 3600*int(m2.group(1)))
    elif not m3 is None:
        # Walk backward until we reach the given day of the week
        day_of_week = days_of_week.index(m3.group(1))
        offset = 1
        while offset <= 7:
            tm = time.gmtime(origin - 86400*offset)
            if tm.tm_wday == day_of_week:
                break
            offset += 1
        else:
            raise ValueError('date "%s" is invalid' % date_str) 
    elif not m4 is None:
        # Walk backward until we reach the given month and year
        month_of_year = months_of_year.index(m4.group(1)) + 1
        day_of_month = int(m4.group(2))
        offset = 1
        while offset <= 366:
            tm = time.gmtime(origin - 86400*offset)
            if tm.tm_mon == month_of_year and tm.tm_mday == day_of_month:
                break
            offset += 1
        else:
            raise ValueError('date "%s" is invalid' % date_str)
    elif not m5 is None:
        # may raise ValueError
        tm = time.strptime(date_str, '%b %d, %Y') 
    else:       
        raise ValueError('date "%s" could not be interpreted' % date_str)
    #return '%d-%02d-%02d' % (tm.tm_year, tm.tm_mon, tm.tm_mday)
    return '%s %d, %d' % (months_of_year[tm.tm_mon], tm.tm_mday, tm.tm_year)
                    


# The HTML can mostly be saved as-is. The main changes we want to make are:
# 1) Remove extraneous <span>s
# 2) Rewrite relative paths ("/Brian-Bi") to full URLs
# 3) Download a copy of each embedded image, including LaTeX.
# 4) Convert Quora's code blocks into actual <code> tags. This is the trickiest
# task of all, because we want to handle both inline and block, and preserve
# the original highlighting.
#
# We won't actually attempt to "decompile" the HTML into the representation
# typed into the answer editor, because if Quora disappears, there won't be
# anything to interpret that anyway.
def cleanup_tree(doc, src, dest):
    for child in src.childNodes:
        if child.nodeType == Node.TEXT_NODE:
            # Text nodes can simply be left as-is
            dest.appendChild(child.cloneNode(False))
            continue
        if child.nodeType != Node.ELEMENT_NODE:
            # ???
            raise ValueError()
        # Otherwise, it's an element node.
        if child.tagName in ['br', 'hr']:
            dest.appendChild(child.cloneNode(False))
        elif child.tagName in ['b', 'i', 'u', 'h1', 'h2', 'ol', 'ul', 'li', 'blockquote', 'wbr', 'p']:
            # This node doesn't need to be modified but its children might.
            new_node = doc.createElement(child.tagName)
            cleanup_tree(doc, child, new_node)
            dest.appendChild(new_node)
        elif 'question_text' in child.getAttribute('class'):
            # enforce h1, which Quora no longer adds
            new_node = doc.createElement('h1')
            new_node.appendChild(child)
            #cleanup_tree(doc, child, new_node)
            dest.appendChild(new_node)
        elif 'board_item_title' in child.getAttribute('class'):
            # enforce h1, which Quora no longer adds
            new_node = doc.createElement('h1')
            new_node.appendChild(child)
            #cleanup_tree(doc, child, new_node)
            dest.appendChild(new_node)
        elif child.getAttribute('data-embed') != '':
            # This is a video. We want to copy the data-embed value, which is HTML for an iframe node.
            # So, we have to parse it into a separate document and import the node.
            iframe_html = child.getAttribute('data-embed')
            parser = HTMLParser(tree=treebuilders.getTreeBuilder('dom'))
            iframe_doc = parser.parse(iframe_html)
            try:
                iframe = iframe_doc.documentElement.childNodes[1].firstChild
                if iframe.tagName != 'iframe':
                    raise ValueError()
                new_node = doc.importNode(iframe, False)
                # Quora uses a protocol-relative URL (//youtube.com/...) so let's make sure we rewrite this.
                src = new_node.getAttribute('src')
                if src.startswith('//'):
                    new_node.setAttribute('src', 'http:' + src)
                # The video will look really bad if we don't explicitly set the dimensions.
                new_node.setAttribute('width', '525')
                new_node.setAttribute('height', '295')
                dest.appendChild(new_node)
            except Exception:
                print('[WARNING] Failed to parse video embed code', file=sys.stderr)
                # Bail out by just copying the original HTML
                dest.appendChild(child.cloneNode(True))
        elif 'inline_codeblock' in child.getAttribute('class'):
            # Inline codeblock. Simply replace this with a <code>.
            try:
                # div > pre > span > (text)
                span = child.firstChild.firstChild
                if span.tagName != 'span':
                    raise ValueError()
                code_element = doc.createElement('code')
                code_element.appendChild(doc.createTextNode(get_text_content(span)))
                dest.appendChild(code_element)
            except ValueError:
                print('[WARNING] Failed to parse inline codeblock', file=sys.stderr)
                # Bail out by just copying the original HTML
                dest.appendChild(child.cloneNode(True))
        elif 'ContentFooter' in child.getAttribute('class') or 'hidden' in child.getAttribute('class'):
            # These are nodes we just want to skip.
            continue
        elif child.tagName in ['span', 'div']:
            # don't insert a span or div; just insert its contents
            cleanup_tree(doc, child, dest)
        # The remaining cases are: link, image (incl. math), and block code.
        elif child.tagName == 'a':
            # A link. We only want to copy the href, and pass the rest through.
            new_node = doc.createElement('a')
            href = child.getAttribute('href')
            if href.startswith('/'):
                href = 'http://quora.com' + href
            new_node.setAttribute('href', href)
            dest.appendChild(new_node)
            cleanup_tree(doc, child, new_node)
        elif child.tagName == 'img':
            src = child.getAttribute('master_src')
            if src == '':
                src = child.getAttribute('src')
            new_node = doc.createElement('img')
            new_node.setAttribute('src', src)
            new_node.setAttribute('alt', child.getAttribute('alt'))
            if args.no_download:
                dest.appendChild(new_node)
                continue
            # Save a copy of the image locally.
            # If an error occurs, just leave the src pointing to Quora.
            try:
                m = re.search('/([^/?]+)(\?|$)', src)
                if m is None:
                    raise ValueError()
                filename = m.group(1)
                if not filename.endswith('.png'):
                    filename += '.png'
                try:
                    img_fd = os.open(args.output_dir + '/' + filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                except OSError as error:
                    if error.errno == errno.EEXIST:
                        log_if_v('Image %s has already been saved; skipping' % filename)
                        new_node.setAttribute('src', filename)
                        continue
                    else:
                        raise
                log_if_v('Downloading image from %s' % src)
                closed = False
                try:
                    img = urllib.request.urlopen(src).read()
                    time.sleep(args.delay)
                    os.write(img_fd, img)
                    os.close(img_fd)
                    closed = True
                except Exception:
                    os.close(img_fd)
                    closed = True
                    try:
                        os.remove(args.output_dir + '/' + filename)
                    except:
                        print('[WARNING] Failed to remove incomplete file %s' % filename, file=sys.stderr)
                    raise
                finally:
                    if not closed:
                        os.close(img_fd)
                    # Don't leave the file there; we will retry it next time.
                # If everything went according to plan, rewrite the src to the local file.
                new_node.setAttribute('src', filename)
            except urllib.error.URLError as error:
                print('[WARNING] Failed to download image from URL %s (%s)' % (src, error.reason), file=sys.stderr)
            except OSError as error:
                print('[WARNING] Failed to save image from URL %s to file %s (%s)' % (src, filename, error.strerror), file=sys.stderr)
            except ValueError:
                print('[WARNING] Failed to determine image name from URL %s' % src, file=sys.stderr)
            finally:
                dest.appendChild(new_node)
        elif 'codeblocktable' in child.getAttribute('class'):
            # Block (not inline) code. This should become <pre><code>...</code></pre>
            try:
                pre_node = doc.createElement('pre')
                # Each div inside is a line.
                code_node = doc.createElement('code')
                divs = child.getElementsByTagName('div')
                lines = []
                for div in divs:
                    # All the code is inside spans.
                    spans = div.getElementsByTagName('span')
                    line = ''.join([get_text_content(span) for span in spans])
                    lines.append(line)
                text_node = doc.createTextNode('\n'.join(lines))
                code_node.appendChild(text_node)
                pre_node.appendChild(code_node)
                dest.appendChild(pre_node)
            except Exception:
                print('[WARNING] Failed to parse code block', file=sys.stderr)
                dest.appendChild(child.cloneNode(True))
        else:
            print('[WARNING] Unrecognized node %s' % child.tagName , file=sys.stderr)
            # Bail out by just copying the original HTML
            dest.appendChild(child.cloneNode(True))

parser = argparse.ArgumentParser(description = 'Convert answers downloaded from Quora into a more portable HTML format')
parser.add_argument('input_dir', nargs='?', default='./quora-answers', help='directory containing "raw" answers downloaded from Quora')
parser.add_argument('output_dir', nargs='?', default='./quora-answers-cooked', help='where to store the images and converted answers')
parser.add_argument('-d', '--delay', default=0, type=float, help='Time to sleep between downloads, in seconds')
parser.add_argument('-n', '--no_download', action='store_true', help='Do not save images')
parser.add_argument('-v', '--verbose', action='store_true', help='be verbose')
parser.add_argument('-t', '--origin_timestamp', default=None, type=int, help='JS time when the list of URLs was fetched')
parser.add_argument('-z', '--origin_timezone', default=None, type=int, help='browser timezone')

global args
args = parser.parse_args()

# Determine the origin for relative date computation
if args.origin_timestamp is None:
    log_if_v('Using current time')
    args.origin_timestamp = time.time()
else:
    args.origin_timestamp //= 1000
if args.origin_timezone is None:
    log_if_v('Using system time zone')
    args.origin_timezone = time.timezone
else:
    args.origin_timezone *= 60
origin = args.origin_timestamp - args.origin_timezone

# Get a list of answers to convert...
filenames = list(filter(lambda f: f.endswith('.html'), os.listdir(args.input_dir)))
filenames.sort()
if len(filenames) == 0:
    sys.exit('[FATAL] No .html files found in directory %s', args.input_dir)
print('Found %d answers' % len(filenames), file=sys.stderr)

log_if_v('Creating directory %s' % args.output_dir)
try:
    os.mkdir(args.output_dir, 0o700)
except OSError as error:
    if error.errno == errno.EEXIST:
        log_if_v('Directory already exists')
    else:
        # This is the top level, and we have nothing else to do if we failed
        raise

for filename in filenames:
    sys.stderr.flush()
    print('Filename: ' + filename, file=sys.stderr)
    try:
        with open(args.input_dir + '/' + filename, 'rb') as page:
            page_html = page.read()
    except IOError as error:
        print('[ERROR] Failed to read %s (%s)' % (filename, error.strerror))
        continue

    #print(page_html)
    # Get the HTML element containing just the answer itself.
    # Also get the title.
    parser = HTMLParser(tree=treebuilders.getTreeBuilder('dom'))
    document = parser.parse(page_html, default_encoding='utf-8')
    title_node = get_title_node(document) 
    log_if_v('Title: ' + ('(could not be determined)' if title_node is None else get_text_content(title_node)))

    answer_node = None
    question_node = None
    post_node = None
    date_node = None
    for node in document.getElementsByTagName('div'):
        #print(node.getAttribute('id'))
        #print(node.getAttribute('class').split())
        if 'ExpandedAnswer' in node.getAttribute('class').split():
            try:
                answer_node = node
            except Exception:
                pass
        elif 'ExpandedPostContent' in node.getAttribute('class').split():
            try:
                post_node = node
            except Exception:
                pass
        elif 'ans_page_question_header' in node.getAttribute('class').split():
            try:
                question_node = node
            except Exception:
                pass
        elif 'BoardItem' in node.getAttribute('class').split():
            try:
                question_node = node
            except Exception:
                pass
        elif 'CredibilityFacts' in node.getAttribute('class').split():
            try:
                date_node = node
                # convert date to consistent MMM, DD, YYYY
                text = date_node.getElementsByTagName('a')[0].firstChild.nodeValue
                matchObj = re.match(r'Answered (.+ ago)', text)
                if matchObj:
                    new_time = parse_quora_date(origin, matchObj.group(1))                    
                    date_node.getElementsByTagName('a')[0].firstChild.nodeValue = 'Answered %s' % new_time
            except Exception:
                pass
        elif 'PostFooter' in node.getAttribute('class').split():
            try:
                date_node = node
                # convert date to consistent MMM, DD, YYYY
                text = date_node.getElementsByTagName('a')[0].firstChild.nodeValue
                matchObj = re.match(r'Posted (.+ ago)', text)
                if matchObj:
                    new_time = parse_quora_date(origin, matchObj.group(1))                    
                    date_node.getElementsByTagName('a')[1].firstChild.nodeValue = 'Posted %s' % new_time
            except Exception:
                pass
    if answer_node is None and post_node is None:
        print('[WARNING] Failed to locate answer on page (Source URL was %s)' % url, file=sys.stderr)
        continue
    if question_node is None:
        print('[WARNING] Failed to locate answer on page (Source URL was %s)' % url, file=sys.stderr)
        continue

    # Construct our new page...
    new_page = document.createElement('html')
    head_node = document.createElement('head')
    if not title_node is None:
        head_node.appendChild(title_node)
    meta_node = document.createElement('meta')
    meta_node.setAttribute('charset', 'utf-8')
    head_node.appendChild(meta_node)
    css = ("blockquote { border-left: 2px solid #ddd; color: #666; margin: 0; padding-left: 16px; } "
           "code, pre { background: #f4f4f4; } "
	   "h1 a { text-decoration:none; } "
           "pre, h2 { margin: 0; } "
	   ".CredibilityFacts { text-align: right; font-style: italic; } "
           "ul { margin: 0 0 0 16px; padding: 8px 0; } "
           "ol { margin: 0 0 0 28px; padding: 8px 0; } "
           "li { margin: 0 0 8px; } ")
    style_node = document.createElement('style')
    style_node.setAttribute('type', 'text/css')
    style_node.appendChild(document.createTextNode(css))
    head_node.appendChild(style_node)
    new_page.appendChild(head_node)
    body_node = document.createElement('body')
    answer_out_node = document.createElement('div')
    question_out_node = document.createElement('div')
    if not question_node is None:
        body_node.appendChild(question_out_node)
    fleuron = document.createTextNode(u'\u2766')
    qn_div = document.createElement('p')
    qn_div.setAttribute('style', 'text-align: center;')
    body_node.appendChild(qn_div)
    qn_div.appendChild(fleuron)
    body_node.appendChild(answer_out_node)
    body_node.appendChild(date_node)
    # This step processes Quora's HTML into a more lightweight and portable form.
    if not question_node is None:
        cleanup_tree(document, question_node, question_out_node)
    if post_node is None:
        cleanup_tree(document, answer_node, answer_out_node)
    else:
        cleanup_tree(document, post_node, answer_out_node)
    new_page.appendChild(body_node)
    # Okay! Finally, save the HTML.
    #walker = treewalkers.getTreeWalker('dom')(new_page)
    try:
        with open(args.output_dir + '/' + filename, 'wb', 0o600) as saved_page:
            #saved_page.write(b'<!DOCTYPE html>')
            #saved_page.write(serializer.htmlserializer.HTMLSerializer(omit_optional_tags=False).render(walker))
            #saved_page.write(serializer.HTMLSerializer(omit_optional_tags=False).render(walker, 'utf-8'))
            saved_page.write(serializer.serialize(new_page, 'dom', 'utf-8', omit_optional_tags=False))
    except IOError as error:
        print('[ERROR] Failed to save to file %s (%s)' % (filename, error.strerror), file=sys.stderr)

print('Done', file=sys.stderr)

