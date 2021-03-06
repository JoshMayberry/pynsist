import importlib
import importlib.abc
import importlib.machinery
import os
import shutil
import sys
import tempfile
import zipfile, zipimport
import fnmatch
from functools import partial

from .util import normalize_path

PY2 = sys.version_info[0] == 2
running_python  = '.'.join(str(x) for x in sys.version_info[:2])

class ExtensionModuleMismatch(ImportError):
    pass

def pjoin(*args, **kwargs):
    newPath = ensurePathFormat(os.path.join(*args, **kwargs))
    return newPath

def ensurePathFormat(oldPath):
    newPath = re.sub("[/\\\\](?!\\\\)", "\\\\\\\\", oldPath)
    return newPath

extensionmod_errmsg = """Found an extension module that will not be usable on %s:
%s
Put Windows packages in pynsist_pkgs/ to avoid this."""

def check_ext_mod(path, target_python):
    """If path is an extension module, check that it matches target platform.
    
    It should be for Windows and we should be running on the same version
    of Python that we're targeting. Raises ExtensionModuleMismatch if not.
    
    Does nothing if path is not an extension module.
    """
    if path.endswith('.so'):
        raise ExtensionModuleMismatch(extensionmod_errmsg % ('Windows', path))
    elif path.endswith('.pyd') and not target_python.startswith(running_python):
        # TODO: From Python 3.2, extension modules can restrict themselves
        # to a stable ABI. Can we detect this?
        raise ExtensionModuleMismatch(extensionmod_errmsg % ('Python '+target_python, path))

def check_package_for_ext_mods(path, target_python, ignore=None):
    """Walk the directory path, calling :func:`check_ext_mod` on each file.
    """
    ignore = ignore or []
    for dirpath, dirnames, filenames in os.walk(path):
        formattedPath = os.path.abspath(os.path.realpath(os.path.join(path, dirpath)))
        if (ignore):
            if (any(os.path.commonpath([item]) == os.path.commonpath([item, formattedPath]) for item in ignore)):
                continue

        for filename in filenames:
            check_ext_mod(os.path.join(formattedPath, filename), target_python)

def copy_zipmodule(loader, modname, target):
    """Copy a module or package out of a zip file to the target directory."""
    file = loader.get_filename(modname)
    assert file.startswith(loader.archive)
    path_in_zip = file[len(loader.archive+'/'):]
    zf = zipfile.ZipFile(loader.archive)

    # If the packages are in a subdirectory, extracting them recreates the
    # directory structure from the zip file. So extract to a temp dir first,
    # and then copy the modules to target.
    tempdir = tempfile.mkdtemp()
    if loader.is_package(modname):
        # Extract everything in a folder
        pkgdir, basename = os.path.split(path_in_zip)
        assert basename.startswith('__init__')
        pkgfiles = [f for f in zf.namelist() if f.startswith(pkgdir)]
        zf.extractall(tempdir, pkgfiles)
        shutil.copytree(pjoin(tempdir, pkgdir), pjoin(target, modname))
    else:
        # Extract a single file
        zf.extract(path_in_zip, tempdir)
        shutil.copy2(pjoin(tempdir, path_in_zip), target)

    shutil.rmtree(tempdir)

def copytree_ignore_callback(excludes, pkgdir, modname, directory, files):
    """This is being called back by our shutil.copytree call to implement the
    'exclude' feature.
    """
    ignored = set()

    # Filter by file names relative to the build directory
    reldir = os.path.relpath(directory, pkgdir)
    target = os.path.join('pkgs', modname, reldir)
    files = [normalize_path(os.path.join(target, fname)) for fname in files]
    # Execute all patterns
    for pattern in excludes + ['*.pyc']:
        ignored.update([
            os.path.basename(fname)
            for fname in fnmatch.filter(files, pattern)
        ])

    return ignored


class ModuleCopier:
    """Finds and copies importable Python modules and packages.

    This is the Python >3.3 version and uses the `importlib` package to
    locate modules.
    """
    def __init__(self, py_version, path=None):
        self.py_version = py_version
        self.path = path if (path is not None) else ([''] + sys.path)

    def copy(self, modname, target, exclude, packages_extraPath=None, packages_ignorePath=None):
        """Copy the importable module 'modname' to the directory 'target'.

        modname should be a top-level import, i.e. without any dots.
        Packages are always copied whole.

        This can currently copy regular filesystem files and directories,
        and extract modules and packages from appropriately structured zip
        files.
        """
        packages_extraPath = packages_extraPath or {}
        packages_ignorePath = packages_ignorePath or {}

        spec = importlib.machinery.PathFinder.find_spec(modname, self.path)
        if spec is None:
            raise ImportError('Could not find %r' % modname)
        loader = spec.loader
        if loader is None:
            raise ImportError('Cannot bundle namespace package %r' % modname)

        pkg = loader.is_package(modname)

        if isinstance(loader, importlib.machinery.ExtensionFileLoader):
            check_ext_mod(loader.path, self.py_version)
            shutil.copy2(loader.path, target)

        elif isinstance(loader, importlib.abc.FileLoader):
            file = loader.get_filename(modname)
            if pkg:
                pkgdir, basename = os.path.split(file)
                assert basename.startswith('__init__')
                check_package_for_ext_mods(pkgdir, self.py_version, ignore = packages_ignorePath.get(modname, None))

                for key, value in packages_extraPath.items():
                    if (os.path.samefile(key, pkgdir)):
                        dest = os.path.join(target, value, modname)
                        break
                else:
                    dest = os.path.join(target, modname)

                if exclude:
                    shutil.copytree(
                        pkgdir, dest,
                        ignore=partial(copytree_ignore_callback, exclude, pkgdir, modname)
                    )
                else:
                    # Don't use our exclude callback if we don't need to,
                    # as it slows things down.
                    shutil.copytree(
                        pkgdir, dest,
                        ignore=shutil.ignore_patterns('*.pyc')
                    )
            else:
                shutil.copy2(file, target)

        elif isinstance(loader, zipimport.zipimporter):
            copy_zipmodule(loader, modname, target)


def copy_modules(modnames, target, py_version, path=None, exclude=None, packages_extraPath=None, packages_ignorePath=None):
    """Copy the specified importable modules to the target directory.

    By default, it finds modules in :data:`sys.path` - this can be overridden
    by passing the path parameter.
    """
    mc = ModuleCopier(py_version, path)
    files_in_target_noext = [os.path.splitext(f)[0] for f in os.listdir(target)]

    # Format ignore path
    if (packages_ignorePath):
        packages_ignorePath = {key: {os.path.abspath(os.path.realpath(item)) for item in value} for key, value in packages_ignorePath.items()}
    else:
        packages_ignorePath = {}

    for modname in modnames:
        if modname in files_in_target_noext:
            # Already there, no need to copy it.
            continue
        mc.copy(modname, target, exclude, packages_extraPath = packages_extraPath, packages_ignorePath = packages_ignorePath)

    if not modnames:
        # NSIS abhors an empty folder, so give it a file to find.
        with open(os.path.join(target, 'placeholder'), 'w') as f:
            f.write('This file only exists so NSIS finds something in this directory.')
