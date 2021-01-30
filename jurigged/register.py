import importlib.util
import logging
import os
import sys
from types import FunctionType

from _frozen_importlib_external import SourceFileLoader

from .codefile import CodeFile
from .utils import EventSource, glob_filter

log = logging.getLogger(__name__)


class Registry:
    def __init__(self):
        # Cache of (module_name, file_contents, mtime)
        # A snapshot of the file contents may be saved before it might be modified
        self.precache = {}
        # Cache of CodeFile (lazy)
        self.cache = {}
        self.precache_activity = EventSource(save_history=True)
        self.activity = EventSource()
        self._log = None

    def set_logger(self, log):
        self._log = log

    def log(self, *args, **kwargs):
        if self._log is not None:
            self._log(*args, **kwargs)

    def prepare(self, module_name, filename):
        if filename not in self.precache and filename not in self.cache:
            with open(filename) as f:
                self.precache[filename] = (
                    module_name,
                    f.read(),
                    os.path.getmtime(filename),
                )
            self.precache_activity.emit(module_name, filename)

    def get(self, filename):
        if filename in self.cache:
            return self.cache[filename]

        if filename in self.precache:
            module_name, cached_source, mtime = self.precache[filename]
            if module_name not in sys.modules:
                return None
            cf = CodeFile(filename, source=cached_source)
            cf.discover(sys.modules[module_name])
            cf.activity.register(self.log)
            # Basic forwarding of the CodeFile's events
            cf.activity.register(self.activity.emit)
            self.cache[filename] = cf
            return cf

        return None

    def auto_register(self, filter=glob_filter("./*.py")):
        def prep(module_name, filename):
            if (
                filename is not None
                and module_name is not None
                and filter(filename)
            ):
                self.prepare(module_name, filename)

        for name, module in sys.modules.items():
            filename = getattr(module, "__file__", None)
            module_name = getattr(module, "__name__", None)
            prep(module_name, filename)

        sniffer = ImportSniffer(prep)
        sniffer.install()
        return sniffer

    def find(self, filename, lineno):
        cf = self.get(filename)
        if cf is None:
            return None, None
        defn = cf.defnmap.get(lineno, None)
        return cf, defn

    def find_function(self, fn):
        if not isinstance(fn, FunctionType):
            return None, None

        co = fn.__code__
        self.prepare(fn.__module__, co.co_filename)
        return self.find(co.co_filename, co.co_firstlineno)


registry = Registry()


class ImportSniffer:
    """A spec finder that simply sniffs for attempted imports.

    Basically we install this at the front of sys.meta_path so that
    importlib.util.find_spec calls it, then we call find_spec
    ourselves to locate the file that's going to be read so that we
    know we have to cache its contents and watch for changes.
    """

    def __init__(self, report):
        self.working = False
        self.report = report

    def install(self):
        sys.meta_path.insert(0, self)

    def uninstall(self):
        sys.meta_path.remove(self)

    def find_module(self, spec, path):
        if not self.working:
            self.working = True
            # We call find_spec ourselves to find out where the file is.
            # This will not cause an infinite loop because self.working
            # is True and we will not enter the conditional. I'm not
            # sure if it's dangerous to call find_spec within find_spec,
            # but it seems to work, so whatever.
            mspec = importlib.util.find_spec(spec, path)
            if (
                mspec is not None
                and isinstance(mspec.loader, SourceFileLoader)
                and mspec.name is not None
                and mspec.origin is not None
            ):
                try:
                    self.report(mspec.name, mspec.origin)
                except Exception as exc:
                    log.error(
                        f"jurigged: Error processing spec {mspec.name}",
                        exc_info=exc,
                    )
            self.working = False
        return None
