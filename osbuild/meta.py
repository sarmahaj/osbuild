"""Introspection and validation for osbuild

This module contains utilities that help to introspect parts
that constitute the inner parts of osbuild, i.e. its stages,
assemblers and sources. Additionally, it provides classes and
functions to do schema validation of OSBuild manifests and
module options.

A central `Index` class can be used to obtain stage and schema
information. For the former a `ModuleInfo` class is returned via
`Index.get_module_info`, which contains meta-information about
the individual stages. Schemata, obtained via `Index.get_schema`
is represented via a `Schema` class that can in turn be used
to validate the individual components.
Additionally, the `Index` also provides meta information about
the different formats and version that are supported to read
manifest descriptions and write output data. Fir this a class
called `FormatInfo` together with `Index.get_format_inf` and
`Index.list_formats` is provided. A `FormatInfo` can also be
inferred for a specific manifest description via a helper
method called `detect_format_info`
"""
import ast
import contextlib
import copy
import importlib.util
import os
import pkgutil
import json
import sys
from collections import deque
from typing import Dict, Iterable, List, Optional

import jsonschema


FAILED_TITLE = "JSON Schema validation failed"
FAILED_TYPEURI = "https://osbuild.org/validation-error"


class ValidationError:
    """Describes a single failed validation

    Consists of a `message` member describing the error
    that occurred and a `path` that points to the element
    that caused the error.
    Implements hashing, equality and less-than and thus
    can be sorted and used in sets and dictionaries.
    """

    def __init__(self, message: str):
        self.message = message
        self.path = deque()

    @classmethod
    def from_exception(cls, ex):
        err = cls(ex.message)
        err.path = ex.absolute_path
        return err

    @property
    def id(self):
        if not self.path:
            return "."

        result = ""
        for p in self.path:
            if isinstance(p, str):
                if " " in p:
                    p = f"'{p}'"
                result += "." + p
            elif isinstance(p, int):
                result += f"[{p}]"
            else:
                raise AssertionError("new type")

        return result

    def as_dict(self):
        """Serializes this object as a dictionary

        The `path` member will be serialized as a list of
        components (string or integer) and `message` the
        human readable message string.
        """
        return {
            "message": self.message,
            "path": list(self.path)
        }

    def rebase(self, path: Iterable[str]):
        """Prepend the `path` to `self.path`"""
        rev = reversed(path)
        self.path.extendleft(rev)

    def __hash__(self):
        return hash((self.id, self.message))

    def __eq__(self, other: "ValidationError"):
        if not isinstance(other, ValidationError):
            raise ValueError("Need ValidationError")

        if self.id != other.id:
            return False
        return self.message == other.message

    def __lt__(self, other: "ValidationError"):
        if not isinstance(other, ValidationError):
            raise ValueError("Need ValidationError")

        return self.id < other.id

    def __str__(self):
        return f"ValidationError: {self.message} [{self.id}]"


class ValidationResult:
    """Result of a JSON Schema validation"""

    def __init__(self, origin: Optional[str]):
        self.origin = origin
        self.errors = set()

    def fail(self, msg: str) -> ValidationError:
        """Add a new `ValidationError` with `msg` as message"""
        err = ValidationError(msg)
        self.errors.add(err)
        return err

    def add(self, err: ValidationError):
        """Add a `ValidationError` to the set of errors"""
        self.errors.add(err)
        return self

    def merge(self, result: "ValidationResult", *, path=None):
        """Merge all errors of `result` into this

        Merge all the errors of in `result` into this,
        adjusting their the paths be pre-pending the
        supplied `path`.
        """
        for err in result:
            err = copy.deepcopy(err)
            err.rebase(path or [])
            self.errors.add(err)

    def as_dict(self):
        """Represent this result as a dictionary

        If there are not errors, returns an empty dict;
        otherwise it will contain a `type`, `title` and
        `errors` field. The `title` is a human readable
        description, the `type` is a URI identifying
        the validation error type and errors is a list
        of `ValueErrors`, in turn serialized as dict.
        Additionally, a `success` member is provided to
        be compatible with pipeline build results.
        """
        errors = [e.as_dict() for e in self]
        if not errors:
            return {}

        return {
            "type": FAILED_TYPEURI,
            "title": FAILED_TITLE,
            "success": False,
            "errors": errors
        }

    @property
    def valid(self):
        """Returns `True` if there are zero errors"""
        return len(self) == 0

    def __iadd__(self, error: ValidationError):
        return self.add(error)

    def __bool__(self):
        return self.valid

    def __len__(self):
        return len(self.errors)

    def __iter__(self):
        return iter(sorted(self.errors))

    def __str__(self):
        return f"ValidationResult: {len(self)} error(s)"

    def __getitem__(self, key):
        if not isinstance(key, str):
            raise ValueError("Only string keys allowed")

        lst = list(filter(lambda e: e.id == key, self))
        if not lst:
            raise IndexError(f"{key} not found")

        return lst


class Schema:
    """JSON Schema representation

    Class that represents a JSON schema. The `data` attribute
    contains the actual schema data itself. The `klass` and
    (optional) `name` refer to entity this schema belongs to.
    The schema information can be used to validate data via
    the `validate` method.

    The class can be created with empty schema data. In that
    case it represents missing schema information. Any call
    to `validate` will then result in a failure.

    The truth value of this objects corresponds to it having
    schema data.
    """

    def __init__(self, schema: str, name: Optional[str] = None):
        self.data = schema
        self.name = name
        self._validator = None

    def check(self) -> ValidationResult:
        """Validate the `schema` data itself"""
        res = ValidationResult(self.name)

        # validator is assigned if and only if the schema
        # itself passes validation (see below). Therefore
        # this can be taken as an indicator for a valid
        # schema and thus we can and should short-circuit
        if self._validator:
            return res

        if not self.data:
            res.fail("missing schema information")
            return res

        try:
            Validator = jsonschema.Draft4Validator
            Validator.check_schema(self.data)
            self._validator = Validator(self.data)
        except jsonschema.exceptions.SchemaError as err:
            res += ValidationError.from_exception(err)

        return res

    def validate(self, target) -> ValidationResult:
        """Validate the `target` against this schema

        If the schema information itself is missing, it
        will return a `ValidationResult` in failed state,
        with 'missing schema information' as the reason.
        """
        res = self.check()
        if not res:
            return res

        for error in self._validator.iter_errors(target):
            res += ValidationError.from_exception(error)

        return res

    def __bool__(self):
        return self.check().valid


class ModuleInfo:
    """Meta information about a stage

    Represents the information about a osbuild pipeline
    modules, like a stage, assembler or source.
    Contains the short description (`desc`), a longer
    description (`info`) and the JSON schema of valid options
    (`opts`). The `validate` method will check a the options
    of a stage instance against the JSON schema.

    Normally this class is instantiated via its `load` method.
    """

    def __init__(self, klass: str, name: str, path: str, info: Dict):
        self.name = name
        self.type = klass
        self.path = path

        opts = info.get("schema") or ""
        self.info = info.get("info")
        self.desc = info.get("desc")
        self.opts = json.loads("{" + opts + "}")

    def get_schema(self):
        schema = {
            "title": f"Pipeline {self.type}",
            "type": "object",
            "additionalProperties": False,
        }

        if self.type in ("Stage", "Assembler"):
            schema["properties"] = {
                "name": {"enum": [self.name]},
                "options": {
                    "type": "object",
                    **self.opts
                }
            }
            schema["required"] = ["name"]
        else:
            schema.update(self.opts)

        # if there are is a definitions node, it needs to be at
        # the top level schema node, since the schema inside the
        # stages is written as-if they were the root node and
        # so are the references
        definitions = self.opts.get("definitions")
        if definitions:
            schema["definitions"] = definitions
            del schema["properties"]["options"]["definitions"]

        return schema

    @classmethod
    def load(cls, root, klass, name) -> Optional["ModuleInfo"]:
        names = ['SCHEMA']

        def value(a):
            v = a.value
            if isinstance(v, ast.Str):
                return v.s
            return ""

        def filter_type(lst, target):
            return [x for x in lst if isinstance(x, target)]

        def targets(a):
            return [t.id for t in filter_type(a.targets, ast.Name)]

        base = cls.module_class_to_directory(klass)
        if not base:
            raise ValueError(f"Unsupported type: {klass}")

        path = os.path.join(root, base, name)
        try:
            with open(path) as f:
                data = f.read()
        except FileNotFoundError:
            return None

        tree = ast.parse(data, name)

        docstring = ast.get_docstring(tree)
        doclist = docstring.split("\n")

        assigns = filter_type(tree.body, ast.Assign)
        targets = [(t, a) for a in assigns for t in targets(a)]
        values = {k: value(v) for k, v in targets if k in names}
        info = {
            'schema': values.get("SCHEMA"),
            'desc': doclist[0],
            'info': "\n".join(doclist[1:])
        }
        return cls(klass, name, path, info)

    @staticmethod
    def module_class_to_directory(klass: str) -> str:
        mapping = {
            "Assembler": "assemblers",
            "Input": "inputs",
            "Source": "sources",
            "Stage": "stages",
        }

        return mapping.get(klass)


class FormatInfo:
    """Meta information about a format

    Class the can be used to get meta information about
    the the different formats in which osbuild accepts
    manifest descriptions and writes results.
    """

    def __init__(self, module):
        self.module = module
        self.version = getattr(module, "VERSION")
        docs = getattr(module, "__doc__")
        info, desc = docs.split("\n", 1)
        self.info = info.strip()
        self.desc = desc.strip()

    @classmethod
    def load(cls, name):
        mod = sys.modules.get(name)
        if not mod:
            mod = importlib.import_module(name)
        if not mod:
            raise ValueError(f"Could not load module {name}")
        return cls(mod)


class Index:
    """Index of modules and formats

    Class that can be used to get the meta information about
    osbuild modules as well as JSON schemata.
    """

    def __init__(self, path: str):
        self.path = path
        self._module_info = {}
        self._format_info = {}
        self._schemata = {}

    @staticmethod
    def list_formats() -> List[str]:
        """List all known formats for manifest descriptions"""
        base = "osbuild.formats"
        spec = importlib.util.find_spec(base)
        locations = spec.submodule_search_locations
        modinfo = [
            mod for mod in pkgutil.walk_packages(locations)
            if not mod.ispkg
        ]

        return [base + "." + m.name for m in modinfo]

    def get_format_info(self, name) -> FormatInfo:
        """Get the `FormatInfo` for the format called `name`"""
        info = self._format_info.get(name)
        if not info:
            info = FormatInfo.load(name)
            self._format_info[name] = info
        return info

    def detect_format_info(self, data) -> Optional[FormatInfo]:
        """Obtain a `FormatInfo` for the format that can handle `data`"""
        formats = self.list_formats()
        version = data.get("version", "1")
        for fmt in formats:
            info = self.get_format_info(fmt)
            if info.version == version:
                return info
        return None

    def list_modules_for_class(self, klass: str) -> List[str]:
        """List all available modules for the given `klass`"""
        module_path = ModuleInfo.module_class_to_directory(klass)

        if not module_path:
            raise ValueError(f"Unsupported nodule class: {klass}")

        path = os.path.join(self.path, module_path)
        modules = filter(lambda f: os.path.isfile(f"{path}/{f}"),
                         os.listdir(path))
        return list(modules)

    def get_module_info(self, klass, name) -> Optional[ModuleInfo]:
        """Obtain `ModuleInfo` for a given stage or assembler"""

        if (klass, name) not in self._module_info:

            info = ModuleInfo.load(self.path, klass, name)
            self._module_info[(klass, name)] = info

        return self._module_info[(klass, name)]

    def get_schema(self, klass, name=None) -> Schema:
        """Obtain a `Schema` for `klass` and `name` (optional)

        Returns a `Schema` for the entity identified via `klass`
        and `name` (if given). Always returns a `Schema` even if
        no schema information could be found for the entity. In
        that case the actual schema data for `Schema` will be
        `None` and any validation will fail.
        """
        schema = self._schemata.get((klass, name))
        if schema is not None:
            return schema

        if klass == "Manifest":
            path = f"{self.path}/schemas/osbuild1.json"
            with contextlib.suppress(FileNotFoundError):
                with open(path, "r") as f:
                    schema = json.load(f)
        elif klass in ["Assembler", "Input", "Source", "Stage"]:
            info = self.get_module_info(klass, name)
            if info:
                schema = info.get_schema()
        else:
            raise ValueError(f"Unknown klass: {klass}")

        schema = Schema(schema, name or klass)
        self._schemata[(klass, name)] = schema

        return schema
