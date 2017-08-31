import datetime
import logging
import re
import sys
from html import escape
from pprint import pformat

import jinja2
import six

from exception_reports.templates import TECHNICAL_500_TEMPLATE
from exception_reports.utils import force_text

logger = logging.getLogger(__name__)


def exception_handler(exc_type, exc_value, traceback):
    ExceptionReporter(exc_type, exc_value, traceback)


def render_exception_report(exception_data):
    jinja_env = jinja2.Environment(loader=jinja2.BaseLoader())
    return jinja_env.from_string(TECHNICAL_500_TEMPLATE).render(exception_data)


class ExceptionReporter(object):
    """
    A class to organize and coordinate reporting on exceptions.
    """

    def __init__(self, exc_type=None, exc_value=None, tb=None):
        self.exc_type = exc_type
        self.exc_value = exc_value
        self.tb = tb

        if not tb:
            self.exc_type, self.exc_value, self.tb = sys.exc_info()

    def get_traceback_data(self):
        """Return a dictionary containing traceback information."""

        frames = self.get_traceback_frames()
        for i, frame in enumerate(frames):
            if 'vars' in frame:
                frame_vars = []
                for k, v in frame['vars']:
                    try:
                        v = pformat(v)
                    except Exception as e:
                        v = repr(e)
                    # The force_escape filter assume unicode, make sure that works
                    if isinstance(v, bytes):
                        v = v.decode('utf-8', 'replace')  # don't choke on non-utf-8 input
                    # Trim large blobs of data
                    if len(v) > 4096:
                        v = '%s... <trimmed %d bytes string>' % (v[0:4096], len(v))
                    frame_vars.append((k, escape(v)))
                frame['vars'] = frame_vars
            frames[i] = frame

        unicode_hint = ''
        if self.exc_type and issubclass(self.exc_type, UnicodeError):
            start = getattr(self.exc_value, 'start', None)
            end = getattr(self.exc_value, 'end', None)
            if start is not None and end is not None:
                unicode_str = self.exc_value.args[1]
                unicode_hint = force_text(
                    unicode_str[max(start - 5, 0):min(end + 5, len(unicode_str))],
                    'ascii', errors='replace'
                )
        c = {
            'unicode_hint': unicode_hint,
            'frames': frames,
            'sys_executable': sys.executable,
            'sys_version_info': '%d.%d.%d' % sys.version_info[0:3],
            'server_time': datetime.datetime.now(datetime.timezone.utc),
            'sys_path': sys.path,
        }
        # Check whether exception info is available
        if self.exc_type:
            c['exception_type'] = self.exc_type.__name__
        if self.exc_value:
            c['exception_value'] = force_text(self.exc_value, errors='replace')
        if frames:
            c['lastframe'] = frames[-1]
        return c

    def get_traceback_html(self):
        """Return HTML version of stack trace"""
        return render_exception_report(self.get_traceback_data())

    def _get_lines_from_file(self, filename, lineno, context_lines, loader=None, module_name=None):
        """
        Returns context_lines before and after lineno from file.
        Returns (pre_context_lineno, pre_context, context_line, post_context).
        """
        source = None
        if loader is not None and hasattr(loader, "get_source"):
            try:
                source = loader.get_source(module_name)
            except ImportError:
                pass
            if source is not None:
                source = source.splitlines()
        if source is None:
            try:
                with open(filename, 'rb') as fp:
                    source = fp.read().splitlines()
            except (OSError, IOError):
                pass
        if source is None:
            return None, [], None, []

        # If we just read the source from a file, or if the loader did not
        # apply tokenize.detect_encoding to decode the source into a Unicode
        # string, then we should do that ourselves.
        if isinstance(source[0], six.binary_type):
            encoding = 'ascii'
            for line in source[:2]:
                # File coding may be specified. Match pattern from PEP-263
                # (http://www.python.org/dev/peps/pep-0263/)
                match = re.search(br'coding[:=]\s*([-\w.]+)', line)
                if match:
                    encoding = match.group(1).decode('ascii')
                    break
            source = [six.text_type(sline, encoding, 'replace') for sline in source]

        lower_bound = max(0, lineno - context_lines)
        upper_bound = lineno + context_lines

        pre_context = source[lower_bound:lineno]
        context_line = source[lineno]
        post_context = source[lineno + 1:upper_bound]

        return lower_bound, pre_context, context_line, post_context

    def get_traceback_frames(self):
        def explicit_or_implicit_cause(exc_value):
            explicit = getattr(exc_value, '__cause__', None)
            implicit = getattr(exc_value, '__context__', None)
            return explicit or implicit

        # Get the exception and all its causes
        exceptions = []
        exc_value = self.exc_value
        while exc_value:
            exceptions.append(exc_value)
            exc_value = explicit_or_implicit_cause(exc_value)

        frames = []
        # No exceptions were supplied to ExceptionReporter
        if not exceptions:
            return frames

        # In case there's just one exception (always in Python 2,
        # sometimes in Python 3), take the traceback from self.tb (Python 2
        # doesn't have a __traceback__ attribute on Exception)
        exc_value = exceptions.pop()
        tb = self.tb if six.PY2 or not exceptions else exc_value.__traceback__

        while tb is not None:
            # Support for __traceback_hide__ which is used by a few libraries
            # to hide internal frames.
            if tb.tb_frame.f_locals.get('__traceback_hide__'):
                tb = tb.tb_next
                continue
            filename = tb.tb_frame.f_code.co_filename
            function = tb.tb_frame.f_code.co_name
            lineno = tb.tb_lineno - 1
            loader = tb.tb_frame.f_globals.get('__loader__')
            module_name = tb.tb_frame.f_globals.get('__name__') or ''
            pre_context_lineno, pre_context, context_line, post_context = self._get_lines_from_file(
                filename, lineno, 7, loader, module_name,
            )
            if pre_context_lineno is not None:
                frames.append({
                    'exc_cause': explicit_or_implicit_cause(exc_value),
                    'exc_cause_explicit': getattr(exc_value, '__cause__', True),
                    'tb': tb,
                    'type': 'django' if module_name.startswith('django.') else 'user',
                    'filename': filename,
                    'function': function,
                    'lineno': lineno + 1,
                    'vars': list(tb.tb_frame.f_locals.items()),
                    'id': id(tb),
                    'pre_context': pre_context,
                    'context_line': context_line,
                    'post_context': post_context,
                    'pre_context_lineno': pre_context_lineno + 1,
                })

            # If the traceback for current exception is consumed, try the
            # other exception.
            if six.PY2:
                tb = tb.tb_next
            elif not tb.tb_next and exceptions:
                exc_value = exceptions.pop()
                tb = exc_value.__traceback__
            else:
                tb = tb.tb_next

        return frames

    def exception_filename(self):
        import uuid
        return str(uuid.uuid4().hex) + str(uuid.uuid4().hex)

    def format_exception(self):
        """
        Return the same data as from traceback.format_exception.
        """
        import traceback
        frames = self.get_traceback_frames()
        tb = [(f['filename'], f['lineno'], f['function'], f['context_line']) for f in frames]
        list = ['Traceback (most recent call last):\n']
        list += traceback.format_list(tb)
        list += traceback.format_exception_only(self.exc_type, self.exc_value)
        return list
