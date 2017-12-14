from __future__ import absolute_import, print_function

import glob
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import warnings
from contextlib import contextmanager
from fnmatch import fnmatch
from subprocess import check_output

from .compat import on_win, default_encoding, find_py_source
from .formats import archive
from .prefixes import SHEBANG_REGEX
from ._progress import progressbar


__all__ = ('CondaPackException', 'CondaEnv', 'pack')


class CondaPackException(Exception):
    """Internal exception to report to user"""
    pass


# String is split so as not to appear in the file bytes unintentionally
PREFIX_PLACEHOLDER = ('/opt/anaconda1anaconda2'
                      'anaconda3')

BIN_DIR = 'Scripts' if on_win else 'bin'

_current_dir = os.path.dirname(__file__)
if on_win:
    raise NotImplementedError("Windows support")
else:
    _scripts = [(os.path.join(_current_dir, 'scripts', 'posix', 'activate'),
                 os.path.join(BIN_DIR, 'activate')),
                (os.path.join(_current_dir, 'scripts', 'posix', 'deactivate'),
                 os.path.join(BIN_DIR, 'deactivate'))]


class _Context(object):
    def __init__(self):
        self.is_cli = False

    def warn(self, msg):
        if self.is_cli:
            print(msg + "\n", file=sys.stderr)
        else:
            warnings.warn(msg)

    @contextmanager
    def set_cli(self):
        old = self.is_cli
        self.is_cli = True
        yield
        self.is_cli = old


context = _Context()


class CondaEnv(object):
    def __init__(self, prefix, files, excluded_files=None):
        self.prefix = prefix
        self.files = files
        self._excluded_files = excluded_files or []

    def __repr__(self):
        return 'CondaEnv<%r, %d files>' % (self.prefix, len(self))

    def __len__(self):
        return len(self.files)

    def __iter__(self):
        return iter(self.files)

    @property
    def name(self):
        """The name of the environment"""
        return os.path.basename(self.prefix)

    @classmethod
    def from_prefix(cls, prefix, **kwargs):
        files = load_environment(prefix, **kwargs)
        return cls(prefix, files)

    @classmethod
    def from_name(cls, name, **kwargs):
        return cls.from_prefix(name_to_prefix(name), **kwargs)

    @classmethod
    def from_default(cls, **kwargs):
        return cls.from_prefix(name_to_prefix(), **kwargs)

    def exclude(self, pattern):
        """Remove all files that match ``pattern``"""
        files = []
        excluded = self._excluded_files.copy()
        include = files.append
        exclude = excluded.append
        for f in self.files:
            if fnmatch(f.target, pattern):
                exclude(f)
            else:
                include(f)
        return CondaEnv(self.prefix, files, excluded)

    def include(self, pattern):
        """Re-add all excluded files that match ``pattern``"""
        files = self.files.copy()
        excluded = []
        include = files.append
        exclude = excluded.append
        for f in self._excluded_files:
            if fnmatch(f.target, pattern):
                include(f)
            else:
                exclude(f)
        return CondaEnv(self.prefix, files, excluded)

    def _output_and_format(self, output, format='infer'):
        if format == 'infer':
            if output is None or output.endswith('.zip'):
                format = 'zip'
            elif output.endswith('.tar.gz') or output.endswith('.tgz'):
                format = 'tar.gz'
            elif output.endswith('.tar.bz2') or output.endswith('.tbz2'):
                format = 'tar.bz2'
            elif output.endswith('.tar'):
                format = 'tar'
            else:
                # Default to zip
                format = 'zip'
        elif format not in {'zip', 'tar.gz', 'tgz', 'tar.bz2', 'tbz2', 'tar'}:
            raise CondaPackException("Unknown format %r" % format)

        if output is None:
            output = os.extsep.join([self.name, format])

        return output, format

    def pack(self, output=None, format='infer', arcroot=None, verbose=False,
             zip_symlinks=False):
        """Package the conda environment into an archive file.

        Parameters
        ----------
        output : str, optional
            The path of the output file. Defaults to the environment name with a
            ``.zip`` suffix (e.g. ``my_env.zip``).
        format : {'infer', 'zip', 'tar.gz', 'tgz', 'tar.bz2', 'tbz2', 'tar'}
            The archival format to use. By default this is inferred by the
            output file extension, falling back to ``zip`` if a non-standard
            extension.
        arcroot : str, optional
            The relative in the archive to the conda environment. Defaults to
            the environment name.
        verbose : bool, optional
            If True, progress is reported to stdout. Default is False.
        zip_symlinks : bool, optional
            Symbolic links aren't supported by the Zip standard, but are
            supported by *many* common Zip implementations. If True, store
            symbolic links in the archive, instead of the file referred to
            by the link. This can avoid storing multiple copies of the same
            files. *Note that the resulting archive may silently fail on
            decompression if the ``unzip`` implementation doesn't support
            symlinks*. Default is False. Ignored if format isn't ``zip``.

        Returns
        -------
        out_path : str
            The path to the zipped environment.
        """
        if not arcroot:
            arcroot = self.name
        else:
            # Ensure the prefix is a relative path
            arcroot = arcroot.strip(os.path.sep)

        # The output path and archive format
        output, format = self._output_and_format(output, format)

        if os.path.exists(output):
            raise CondaPackException("File %r already exists" % output)

        if verbose:
            print("Packing environment at %r to %r" % (self.prefix, output))

        fd, temp_path = tempfile.mkstemp()

        try:
            with open(fd, 'wb') as temp_file:
                with archive(temp_file, arcroot, format,
                             zip_symlinks=zip_symlinks) as arc:
                    packer = Packer(self.prefix, arc)
                    with progressbar(self.files, enabled=verbose) as files:
                        for f in files:
                            packer.add(f)
                        packer.finish()

        except Exception:
            # Writing failed, remove tempfile
            os.remove(temp_path)
            raise
        else:
            # Writing succeeded, move archive to desired location
            shutil.move(temp_path, output)

        return output


class File(object):
    """A single archive record.

    Parameters
    ----------
    source : str
        Absolute path to the source.
    target : str
        Relative path from the target prefix (e.g. ``lib/foo/bar.py``).
    is_conda : bool, optional
        Whether the file was installed by conda, or comes from somewhere else.
    file_mode : {None, 'text', 'binary', 'unknown'}, optional
        The type of record.
    prefix_placeholder : None or str, optional
        The prefix placeholder in the file (if any)
    """
    __slots__ = ('source', 'target', 'is_conda', 'file_mode',
                 'prefix_placeholder')

    def __init__(self, source, target, is_conda=True, file_mode=None,
                 prefix_placeholder=None):
        self.source = source
        self.target = target
        self.is_conda = is_conda
        self.file_mode = file_mode
        self.prefix_placeholder = prefix_placeholder

    def __repr__(self):
        return 'File<%r, is_conda=%r>' % (self.target, self.is_conda)


def pack(name=None, prefix=None, output=None, format='infer',
         arcroot=None, verbose=False, zip_symlinks=False, filters=None):
    """Package an existing conda environment into an archive file.

    Parameters
    ----------
    name : str, optional
        The name of the conda environment to pack.
    prefix : str, optional
        A path to a conda environment to pack.
    output : str, optional
        The path of the output file. Defaults to the environment name with a
        ``.zip`` suffix (e.g. ``my_env.zip``).
    format : {'infer', 'zip', 'tar.gz', 'tgz', 'tar.bz2', 'tbz2', 'tar'}, optional
        The archival format to use. By default this is inferred by the output
        file extension, falling back to `zip` if a non-standard extension.
    arcroot : str, optional
        The relative in the archive to the conda environment. Defaults to the
        environment name.
    verbose : bool, optional
        If True, progress is reported to stdout. Default is False.
    zip_symlinks : bool, optional
        Symbolic links aren't supported by the Zip standard, but are supported
        by *many* common Zip implementations. If True, store symbolic links in
        the archive, instead of the file referred to by the link. This can
        avoid storing multiple copies of the same files. *Note that the
        resulting archive may silently fail on decompression if the ``unzip``
        implementation doesn't support symlinks*. Default is False. Ignored if
        format isn't ``zip``.
    filters : list, optional
        A list of filters to apply to the files. Each filter is a tuple of
        ``(kind, pattern)``, where ``kind`` is either ``'exclude'`` or
        ``'include'`` and ``pattern`` is a file pattern. Filters are applied in
        the order specified.

    Returns
    -------
    out_path : str
        The path to the zipped environment.
    """
    if name and prefix:
        raise CondaPackException("Cannot specify both ``name`` and ``prefix``")

    if verbose:
        print("Collecting packages...")

    if prefix:
        env = CondaEnv.from_prefix(prefix)
    elif name:
        env = CondaEnv.from_name(name)
    else:
        env = CondaEnv.from_default()

    if filters is not None:
        for kind, pattern in filters:
            if kind == 'exclude':
                env = env.exclude(pattern)
            elif kind == 'include':
                env = env.include(pattern)
            else:
                raise CondaPackException("Unknown filter of kind %r" % kind)

    return env.pack(output=output, format=format, arcroot=arcroot,
                    verbose=verbose, zip_symlinks=zip_symlinks)


def find_site_packages(prefix):
    # Ensure there is exactly one version of python installed
    pythons = []
    for fn in glob.glob(os.path.join(prefix, 'conda-meta', 'python-*.json')):
        with open(fn) as fil:
            meta = json.load(fil)
        if meta['name'] == 'python':
            pythons.append(meta)

    if len(pythons) > 1:
        raise CondaPackException("Unexpected failure, multiple versions of "
                                 "python found in prefix %r" % prefix)

    elif not pythons:
        raise CondaPackException("Unexpected failure, no version of python "
                                 "found in prefix %r" % prefix)

    # Only a single version of python installed in this environment
    if on_win:
        return 'Lib/site-packages'

    python_version = pythons[0]['version']
    major_minor = python_version[:3]  # e.g. '3.5.1'[:3]

    return 'lib/python%s/site-packages' % major_minor


def check_no_editable_packages(prefix, site_packages):
    pth_files = glob.glob(os.path.join(prefix, site_packages, '*.pth'))
    editable_packages = set()
    for pth_fil in pth_files:
        dirname = os.path.dirname(pth_fil)
        with open(pth_fil) as pth:
            for line in pth:
                if line.startswith('#'):
                    continue
                line = line.rstrip()
                if line:
                    location = os.path.normpath(os.path.join(dirname, line))
                    if not location.startswith(prefix):
                        editable_packages.add(line)
    if editable_packages:
        msg = ("Cannot pack an environment with editable packages\n"
               "installed (e.g. from `python setup.py develop` or\n "
               "`pip install -e`). Editable packages found:\n\n"
               "%s") % '\n'.join('- %s' % p for p in sorted(editable_packages))
        raise CondaPackException(msg)


def name_to_prefix(name=None):
    info = check_output("conda info --json", shell=True).decode(default_encoding)
    info2 = json.loads(info)

    if name:
        env_lk = {os.path.basename(e): e for e in info2['envs']}
        try:
            prefix = env_lk[name]
        except KeyError:
            raise CondaPackException("Environment name %r doesn't exist" % name)
    else:
        prefix = info2['default_prefix']

    return prefix


def read_noarch_type(pkg):
    for file_name in ['link.json', 'package_metadata.json']:
        path = os.path.join(pkg, 'info', file_name)
        if os.path.exists(path):
            with open(path) as fil:
                info = json.load(fil)
            try:
                return info['noarch']['type']
            except KeyError:
                return None
    return None


def read_has_prefix(path):
    out = {}
    with open(path) as fil:
        for line in fil:
            rec = tuple(x.strip('"\'') for x in shlex.split(line, posix=False))
            if len(rec) == 1:
                out[rec[0]] = (PREFIX_PLACEHOLDER, 'text')
            elif len(rec) == 3:
                out[rec[2]] = rec[:2]
            else:
                raise ValueError("Failed to parse has_prefix file")
    return out


def collect_unmanaged(prefix, managed):
    from os.path import relpath, join, isfile, islink

    remove = {join('bin', f) for f in ['conda', 'activate', 'deactivate']}

    ignore = {'pkgs', 'envs', 'conda-bld', 'conda-meta', '.conda_lock',
              'users', 'LICENSE.txt', 'info', 'conda-recipes', '.index',
              '.unionfs', '.nonadmin', 'python.app', 'Launcher.app'}

    res = set()

    for fn in os.listdir(prefix):
        if fn in ignore:
            continue
        elif isfile(join(prefix, fn)):
            res.add(fn)
        else:
            for root, dirs, files in os.walk(join(prefix, fn)):
                root2 = relpath(root, prefix)
                res.update(join(root2, fn2) for fn2 in files)

                for d in dirs:
                    if islink(join(root, d)):
                        # Add symbolic directory directly
                        res.add(join(root2, d))

                if not dirs and not files:
                    # root2 is an empty directory, add it
                    res.add(root2)

    managed = {i.target for i in managed}
    res -= managed
    res -= remove

    return [File(os.path.join(prefix, p), p, is_conda=False,
                 prefix_placeholder=None, file_mode='unknown')
            for p in res if not (p.endswith('~') or
                                 p.endswith('.DS_Store') or
                                 (find_py_source(p) in managed))]


def managed_file(is_noarch, site_packages, pkg, _path, prefix_placeholder=None,
                 file_mode=None, **ignored):
    if is_noarch:
        if _path.startswith('site-packages/'):
            target = site_packages + _path[13:]
        elif _path.startswith('python-scripts/'):
            target = BIN_DIR + _path[14:]
        else:
            target = _path
    else:
        target = _path

    return File(os.path.join(pkg, _path),
                target,
                is_conda=True,
                prefix_placeholder=prefix_placeholder,
                file_mode=file_mode)


def load_managed_package(info, prefix, site_packages):
    pkg = info['link']['source']

    noarch_type = read_noarch_type(pkg)

    is_noarch = noarch_type == 'python'

    paths_json = os.path.join(pkg, 'info', 'paths.json')
    if os.path.exists(paths_json):
        with open(paths_json) as fil:
            paths = json.load(fil)

        files = [managed_file(is_noarch, site_packages, pkg, **r)
                 for r in paths['paths']]
    else:
        with open(os.path.join(pkg, 'info', 'files')) as fil:
            paths = [f.strip() for f in fil]

        has_prefix = os.path.join(pkg, 'info', 'has_prefix')

        if os.path.exists(has_prefix):
            prefixes = read_has_prefix(has_prefix)
            files = [managed_file(is_noarch, site_packages, pkg, p,
                                  *prefixes.get(p, ())) for p in paths]
        else:
            files = [managed_file(is_noarch, site_packages, pkg, p)
                     for p in paths]

    if noarch_type == 'python':
        seen = {i.target for i in files}
        for fil in info['files']:
            if fil not in seen:
                file_mode = 'unknown' if fil.startswith(BIN_DIR) else None
                f = File(os.path.join(prefix, fil), fil, is_conda=True,
                         prefix_placeholder=None, file_mode=file_mode)
                files.append(f)
    return files


_uncached_error = """
Conda-managed packages were found without entries in the package cache. This
is usually due to `conda clean -p` being unaware of symlinked or copied
packages. Uncached packages:

{0}"""

_uncached_warning = """\
{0}

Continuing with packing, treating these packages as if they were unmanaged
files (e.g. from `pip`). This is usually fine, but may cause issues as
prefixes aren't be handled as robustly.""".format(_uncached_error)


def load_environment(prefix, unmanaged=True, on_missing_cache='warn'):
    # Check if it's a conda environment
    if not os.path.exists(prefix):
        raise CondaPackException("Environment path %r doesn't exist" % prefix)
    conda_meta = os.path.join(prefix, 'conda-meta')
    if not os.path.exists(conda_meta):
        raise CondaPackException("Path %r is not a conda environment" % prefix)

    # Find the environment site_packages (if any)
    site_packages = find_site_packages(prefix)

    # Check that no editable packages are installed
    check_no_editable_packages(prefix, site_packages)

    files = []
    uncached = []
    for path in os.listdir(conda_meta):
        if path.endswith('.json'):
            with open(os.path.join(conda_meta, path)) as fil:
                info = json.load(fil)
            pkg = info['link']['source']

            if not os.path.exists(pkg):
                # Package cache is cleared, set file_mode='unknown' to properly
                # handle prefix replacement ourselves later.
                new_files = [File(os.path.join(prefix, f), f, is_conda=True,
                                  prefix_placeholder=None, file_mode='unknown')
                             for f in info['files']]
                uncached.append((info['name'], info['version'], info['url']))
            else:
                new_files = load_managed_package(info, prefix, site_packages)

            files.extend(new_files)

    if unmanaged:
        files.extend(collect_unmanaged(prefix, files))

    # Add activate/deactivate scripts
    files.extend(File(*s) for s in _scripts)

    if uncached and on_missing_cache in ('warn', 'raise'):
        packages = '\n'.join('- %s=%r   %s' % i for i in uncached)
        if on_missing_cache == 'warn':
            context.warn(_uncached_warning.format(packages))
        else:
            raise CondaPackException(_uncached_error.format(packages))

    return files


def strip_prefix(data, prefix, placeholder=PREFIX_PLACEHOLDER):
    try:
        s = data.decode('utf-8')
        if prefix in s:
            data = s.replace(prefix, placeholder).encode('utf-8')
        else:
            placeholder = None
    except UnicodeDecodeError:  # data is binary
        placeholder = None

    return data, placeholder


def rewrite_shebang(data, target, prefix):
    """Rewrite a shebang header to ``#!usr/bin/env program...``.

    Returns
    -------
    data : bytes
    fixed : bool
        Whether the file was successfully fixed in the rewrite.
    """
    shebang_match = re.match(SHEBANG_REGEX, data, re.MULTILINE)
    prefix_b = prefix.encode('utf-8')

    if shebang_match:
        if data.count(prefix_b) > 1:
            # More than one occurrence of prefix, can't fully cleanup.
            return data, False

        shebang, executable, options = shebang_match.groups()

        if executable.startswith(prefix_b):
            # shebang points inside environment, rewrite
            executable_name = executable.decode('utf-8').split('/')[-1]
            new_shebang = '#!/usr/bin/env %s%s' % (executable_name,
                                                   options.decode('utf-8'))
            data = data.replace(shebang, new_shebang.encode('utf-8'))

        return data, True

    return data, False


_conda_unpack_template = """\
{shebang}
{prefixes_py}

_prefix_records = [
{prefix_records}
]

if __name__ == '__main__':
    import os
    script_dir = os.path.dirname(__file__)
    new_prefix = os.path.dirname(script_dir)
    for path, placeholder, mode in _prefix_records:
        update_prefix(os.path.join(new_prefix, path), new_prefix,
                      placeholder, mode=mode)
"""


class Packer(object):
    def __init__(self, prefix, archive):
        self.prefix = prefix
        self.archive = archive
        self.prefixes = []

    def add(self, file):
        if file.file_mode is None:
            self.archive.add(file.source, file.target)

        elif os.path.isdir(file.source) or os.path.islink(file.source):
            self.archive.add(file.source, file.target)

        elif file.file_mode == 'unknown':
            with open(file.source, 'rb') as fil:
                data = fil.read()

            data, prefix_placeholder = strip_prefix(data, self.prefix)

            if prefix_placeholder is not None:
                if file.target.startswith(BIN_DIR):
                    data, fixed = rewrite_shebang(data, file.target,
                                                  prefix_placeholder)
                else:
                    fixed = False

                if not fixed:
                    self.prefixes.append((file.target, prefix_placeholder, 'text'))
            self.archive.add_bytes(file.source, data, file.target)

        elif file.file_mode == 'text':
            if file.target.startswith(BIN_DIR):
                with open(file.source, 'rb') as fil:
                    data = fil.read()

                data, fixed = rewrite_shebang(data, file.target, file.prefix_placeholder)
                self.archive.add_bytes(file.source, data, file.target)
                if not fixed:
                    self.prefixes.append((file.target, file.prefix_placeholder, 'text'))
            else:
                self.archive.add(file.source, file.target)
                self.prefixes.append((file.target, file.prefix_placeholder,
                                      file.file_mode))

        elif file.file_mode == 'binary':
            self.archive.add(file.source, file.target)
            self.prefixes.append((file.target, file.prefix_placeholder, file.file_mode))

        else:
            raise ValueError("unknown file_mode: %r" % file.file_mode)

    def finish(self):
        if not on_win:
            shebang = '#!/usr/bin/env python'
        else:
            shebang = ('@SETLOCAL ENABLEDELAYEDEXPANSION & CALL "%~f0" & (IF '
                       'NOT ERRORLEVEL 1 (python -x "%~f0" %*) ELSE (ECHO No '
                       'python environment found on path)) & PAUSE & EXIT /B '
                       '!ERRORLEVEL!')

        prefix_records = ',\n'.join(repr(p) for p in self.prefixes)

        with open(os.path.join(_current_dir, 'prefixes.py')) as fil:
            prefixes_py = fil.read()

        script = _conda_unpack_template.format(shebang=shebang,
                                               prefix_records=prefix_records,
                                               prefixes_py=prefixes_py)

        with tempfile.NamedTemporaryFile(mode='w') as fil:
            fil.write(script)
            fil.flush()
            st = os.stat(fil.name)
            os.chmod(fil.name, st.st_mode | 0o111)  # make executable
            self.archive.add(fil.name, os.path.join(BIN_DIR, 'conda-unpack'))