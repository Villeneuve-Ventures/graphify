"""Shared interpreter identity-screening policy for generated commands and hooks."""

_GRAPHIFY_IDENTITY_SOURCE = r'''import sys
if sys.flags.no_site != 1 and sys.argv[1] != "trusted": raise SystemExit(1)
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
import site
import stat
import urllib.parse
import urllib.request

def contained(path, root):
    try:
        normalized_root = os.path.normcase(root)
        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root
    except (OSError, ValueError):
        return False

def unique_paths(paths):
    result = []
    seen = set()
    for path in paths:
        if not isinstance(path, str) or not path:
            raise ValueError
        absolute = os.path.abspath(path)
        key = os.path.normcase(absolute)
        if key not in seen:
            seen.add(key)
            result.append(absolute)
    return result

def venv_system_site_enabled():
    config = os.path.join(sys.prefix, "pyvenv.cfg")
    try:
        text = open(config, encoding="utf-8").read()
    except (OSError, UnicodeError):
        raise ValueError
    values = []
    for raw_line in text.splitlines():
        key, separator, value = raw_line.partition("=")
        if key.strip().lower() != "include-system-site-packages":
            continue
        if not separator or value.strip().lower() not in ("true", "false"):
            raise ValueError
        values.append(value.strip().lower() == "true")
    if len(values) != 1:
        raise ValueError
    return values[0]

def normal_site_roots():
    roots = []
    is_venv = sys.prefix != sys.base_prefix or sys.exec_prefix != sys.base_exec_prefix
    user_enabled = False
    if is_venv:
        roots.extend(site.getsitepackages(unique_paths((sys.prefix, sys.exec_prefix))))
        include_system = venv_system_site_enabled()
        if include_system:
            user_enabled = site.check_enableusersite() is True
            if user_enabled:
                roots.append(site.getusersitepackages())
            roots.extend(
                site.getsitepackages(
                    unique_paths((sys.base_prefix, sys.base_exec_prefix))
                )
            )
    else:
        user_enabled = site.check_enableusersite() is True
        if user_enabled:
            roots.append(site.getusersitepackages())
        roots.extend(site.getsitepackages(unique_paths((sys.prefix, sys.exec_prefix))))
    return [path for path in unique_paths(roots) if os.path.isdir(path)], user_enabled

def path_denied(path, deny_roots):
    absolute = os.path.abspath(path)
    real = os.path.realpath(absolute)
    for root_arg in deny_roots:
        if not root_arg:
            continue
        root = os.path.abspath(root_arg)
        real_root = os.path.realpath(root)
        if contained(absolute, root) or contained(real, real_root):
            return True
    return False

def inert_startup_paths(roots, deny_roots, strict):
    accepted = []
    unsafe = False
    for root in roots:
        try:
            entries = sorted(os.scandir(root), key=lambda entry: entry.name)
        except OSError:
            raise ValueError
        for entry in entries:
            if entry.name.startswith(".") or not entry.name.endswith(".pth"):
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
                if (
                    getattr(entry_stat, "st_flags", 0) & getattr(stat, "UF_HIDDEN", 0)
                    or getattr(entry_stat, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2)
                ):
                    continue
                if (
                    entry.is_symlink()
                    or not entry.is_file(follow_symlinks=False)
                ):
                    unsafe = True
                    continue
                text = open(entry.path, encoding="utf-8-sig").read()
            except (OSError, UnicodeError):
                unsafe = True
                continue
            file_accepted = []
            file_unsafe = False
            for raw_line in text.splitlines():
                line = raw_line.rstrip()
                if not line or line.startswith("#"):
                    continue
                if "\x00" in line or line.startswith(("import ", "import\t")):
                    file_unsafe = True
                    continue
                target = os.path.abspath(os.path.join(root, line))
                if not os.path.exists(target):
                    continue
                try:
                    target_mode = os.stat(target).st_mode
                except OSError:
                    file_unsafe = True
                    continue
                if not (stat.S_ISDIR(target_mode) or stat.S_ISREG(target_mode)):
                    file_unsafe = True
                    continue
                if path_denied(target, deny_roots):
                    file_unsafe = True
                    continue
                file_accepted.append(target)
            if file_unsafe:
                unsafe = True
            else:
                accepted.extend(file_accepted)
    if strict and unsafe:
        raise ValueError
    return unique_paths(accepted)

def ambient_paths(deny_roots, strict):
    roots, user_enabled = normal_site_roots()
    sanitized = list(sys.path)
    for root in roots:
        sanitized.extend((root, *inert_startup_paths([root], deny_roots, strict)))
    sanitized = unique_paths(sanitized)
    if strict:
        if importlib.machinery.PathFinder.find_spec("sitecustomize", sanitized) is not None:
            raise ValueError
        if user_enabled and importlib.machinery.PathFinder.find_spec(
            "usercustomize", sanitized
        ) is not None:
            raise ValueError
    return roots, sanitized

def supported():
    return (
        sys.implementation.name == "cpython"
        and sys.version_info.releaselevel == "final"
        and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0)
    )

try:
    arguments = sys.argv[1:]
    action = arguments[0]
    if not supported():
        raise ValueError
    if action == "executable":
        print(sys.executable)
        raise SystemExit(0)
    if action == "trusted":
        deny_roots = []
        distribution = importlib.metadata.distribution("graphifyy")
        if distribution.metadata.get("Name") != "graphifyy":
            raise ValueError
    elif action not in ("ambient-supported", "ambient-identity"):
        raise ValueError
    else:
        deny_roots = arguments[1:]
        roots, sanitized = ambient_paths(
            deny_roots, strict=action == "ambient-supported"
        )
        if action == "ambient-supported":
            raise SystemExit(0)
        sys.path[:] = sanitized
        distribution = next(
            distribution
            for distribution in importlib.metadata.distributions(path=roots)
            if distribution.metadata.get("Name") == "graphifyy"
        )
    spec = importlib.util.find_spec("graphify")
    if spec is None or not spec.origin:
        raise ValueError
    origin = os.path.abspath(spec.origin)
    real_origin = os.path.realpath(origin)
    direct_url_text = distribution.read_text("direct_url.json")
    editable = False
    if direct_url_text is not None:
        direct_url = json.loads(direct_url_text)
        parsed = urllib.parse.urlparse(direct_url["url"])
        if direct_url.get("dir_info", {}).get("editable") is True:
            editable = True
            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                raise ValueError
            package_root = os.path.abspath(
                urllib.request.url2pathname(parsed.path)
            )
    if editable:
        real_package_root = os.path.realpath(package_root)
        if not contained(origin, package_root) or not contained(real_origin, real_package_root):
            raise ValueError
    else:
        owned = [
            entry
            for entry in (distribution.files or ())
            if str(entry) == "graphify/__init__.py"
        ]
        if len(owned) != 1:
            raise ValueError
        recorded_origin = os.path.abspath(distribution.locate_file(owned[0]))
        if os.path.normcase(recorded_origin) != os.path.normcase(origin):
            raise ValueError
    for root_arg in deny_roots:
        if not root_arg:
            continue
        root = os.path.abspath(root_arg)
        real_root = os.path.realpath(root)
        if contained(origin, root) or contained(real_origin, real_root):
            raise ValueError
except (Exception, SystemExit) as error:
    if isinstance(error, SystemExit) and error.code == 0:
        raise
    raise SystemExit(1)
'''
