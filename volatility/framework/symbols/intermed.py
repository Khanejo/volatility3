import copy
import json
import logging
import lzma
import os
import pathlib
import urllib.parse

from volatility import schemas
from volatility.framework import class_subclasses, constants, exceptions, interfaces, objects
from volatility.framework.exceptions import SymbolSpaceError
from volatility.framework.symbols import native

vollog = logging.getLogger(__name__)


# ## TODO
#
# All symbol tables should take a label to an object template
#
# Templates for subtypes etc should be looked up recursively just like anything else
# We therefore need a way to unroll rolled-up types
# Generate mangled names on the fly (prohibits external calling)
#
# Symbol list could be a dict with knowledge of its parent?
# Class split is arbitrary, it's an extension for developers
# Object template should contain both class and initial parameters
#
#
# *** Resolution should not happen in the resolve function
# It should only happen on access of contained types ***
#
# Recursive objects can be fixed by having caching the objects
# (however, they have to be built first!)
#
# Single hop resolution is probably the solution
# Could probably deal with it by having a property that caches
# for container types
#

def _construct_delegate_function(name, is_property = False):
    def _delegate_function(self, *args, **kwargs):
        if is_property:
            return getattr(self._delegate, name)
        return getattr(self._delegate, name)(*args, **kwargs)

    if is_property:
        return property(_delegate_function)
    return _delegate_function


class IntermediateSymbolTable(interfaces.symbols.SymbolTableInterface):
    def __init__(self, context, config_path, name, isf_filepath, native_types = None):
        # Check there are no obvious errors
        url = urllib.parse.urlparse(isf_filepath)
        if url.scheme != 'file':
            raise NotImplementedError(
                "This scheme is not yet implement for the Intermediate Symbol Format: {}".format(url.scheme))

        # Open the file and test the version
        self._versions = dict([(x.version, x) for x in class_subclasses(ISFormatTable)])
        if url.path.endswith('.xz'):
            fp = lzma.open(url.path, 'rt')
        else:
            fp = open(url.path, "r")
        json_object = json.load(fp)
        fp.close()

        # Validation is expensive, but we cache to store the hashes of successfully validated json objects
        if not schemas.validate(json_object):
            raise SymbolSpaceError("File does not pass version validation: {}".format(url.geturl()))

        metadata = json_object.get('metadata', None)

        # Determine the delegate or throw an exception
        self._delegate = self._closest_version(metadata.get('format', "0.0.0"), self._versions)(context,
                                                                                                config_path,
                                                                                                name,
                                                                                                json_object,
                                                                                                native_types)

        # Inherit
        super().__init__(context, config_path, name, native_types or self._delegate.natives)

    @classmethod
    def open(cls, context, config_path, name, relative_isf, native_types = None):
        """Constructs an IntermediateSymbolTable based on a relative path rather than a full URL

        This will attempt to load .json and .json.xz files
        """
        base_core = os.path.dirname(__file__)
        base_user = os.path.join(os.path.dirname(os.path.dirname(base_core)), 'symbols')
        search_paths = [base_user, base_core]
        isf_url = None
        for path in search_paths:
            # Favour specific name, over uncompressed JSON (user-editable), over compressed JSON over uncompressed files
            for extension in ['', '.json', '.json.xz']:
                test = os.path.join(path, relative_isf + extension)
                if os.path.exists(test):
                    isf_url = pathlib.Path(test).as_uri()
                    break
            if isf_url:
                break
        else:
            raise FileNotFoundError(
                "ISF path fragment {} could not be found in standard search paths".format(relative_isf))
        return cls(context, config_path, name, isf_url, native_types)

    def _closest_version(self, version, versions):
        """Determines the highest suitable handler for specified version format
        
        An interface version such as Current.Age.Revision means that (Current - Age) of the provider must be equal to that of the
          consumer, and the provider (the JSON in this instance) must have a greater age (indicating that only additive
          changes have been made) than the consumer (in this case, the file reader).
        """
        supported, age, revision = [int(x) for x in version.split(".")]
        supported_versions = [x for x in versions.keys() if x[0] == (supported - age) and x[1] >= age]
        if not supported_versions:
            raise ValueError(
                "No Intermediate Format interface versions support file interface version: {}".format(version))
        return versions[max(supported_versions)]

    symbols = _construct_delegate_function('symbols', True)
    types = _construct_delegate_function('types', True)
    enumerations = _construct_delegate_function('enumerations', True)
    get_type = _construct_delegate_function('get_type')
    get_symbol = _construct_delegate_function('get_symbol')
    get_enumeration = _construct_delegate_function('get_enumeration')
    get_type_class = _construct_delegate_function('get_type_class')
    set_type_class = _construct_delegate_function('set_type_class')
    del_type_class = _construct_delegate_function('del_type_class')


class ISFormatTable(interfaces.symbols.SymbolTableInterface):
    """Provide a base class to identify all subclasses"""
    pass


class Version1Format(ISFormatTable):
    """Class for storing intermediate debugging data as objects and classes"""
    current = 1
    revision = 0
    age = 1
    version = (current - age, age, revision)

    def __init__(self, context, config_path, name, json_object, native_types = None):
        self._json_object = json_object
        self._validate_json()
        nt = native_types or self._get_natives()
        super().__init__(context, config_path, name, nt)
        self._overrides = {}
        self._symbol_cache = None

    def _get_natives(self):
        """Determines the appropriate native_types to use from the JSON data"""
        # TODO: Consider how to generate the natives entirely from the ISF
        classes = {"x64": native.x64NativeTable, "x86": native.x86NativeTable}
        for nc in sorted(classes):
            native_class = classes[nc]
            for base_type in self._json_object['base_types']:
                try:
                    if self._json_object['base_types'][base_type]['length'] != native_class.get_type(base_type).size:
                        break
                except TypeError:
                    # TODO: determine whether we should give voids a size - We don't give voids a length, whereas microsoft seemingly do
                    pass
            else:
                vollog.debug("Choosing appropriate natives for symbol library: {}".format(nc))
                return native_class.natives

    # TODO: Check the format and make use of the other metadata

    def _validate_json(self):
        if (not 'user_types' in self._json_object or
                not 'base_types' in self._json_object or
                not 'metadata' in self._json_object or
                not 'symbols' in self._json_object or
                not 'enums' in self._json_object):
            raise exceptions.SymbolSpaceError("Malformed JSON file provided")

    def get_symbol(self, name):
        """Returns the location offset given by the symbol name"""
        symbol = self._json_object['symbols'].get(name, None)
        if not symbol:
            raise KeyError("Unknown symbol: {}".format(name))
        return interfaces.symbols.Symbol(name = name, address = symbol['address'])

    @property
    def symbols(self):
        if not self._symbol_cache:
            self._symbol_cache = [self.get_symbol(x) for x in self._json_object['symbols']]
        return self._symbol_cache

    # TODO: Add the ability to add/remove/change symbols after creation, note that this should invalidate the cache

    @property
    def enumerations(self):
        """Returns an iterator of the available enumerations"""
        return self._json_object.get('enums', {}).keys()

    def get_type_class(self, name):
        return self._overrides.get(name, objects.Struct)

    def set_type_class(self, name, clazz):
        if name not in self.types:
            raise ValueError("Symbol type not in {} SymbolTable: {}".format(self.name, name))
        self._overrides[name] = clazz

    def del_type_class(self, name):
        if name in self._overrides:
            del self._overrides[name]

    @property
    def types(self):
        """Returns an iterator of the symbol names"""
        return list(self._json_object.get('user_types', {}).keys()) + list(self.natives.types)

    def _interdict_to_template(self, dictionary):
        """Converts an intermediate format dict into an object template"""
        if not dictionary:
            raise exceptions.SymbolSpaceError("Invalid intermediate dictionary: {}".format(dictionary))

        type_name = dictionary['kind']
        if type_name == 'base':
            type_name = dictionary['name']

        if type_name in self.natives.types:
            # The symbol is a native type
            native_template = self.natives.get_type(self.name + constants.BANG + type_name)

            # Add specific additional parameters, etc
            update = {}
            if type_name == 'array':
                update['count'] = dictionary['count']
                update['subtype'] = self._interdict_to_template(dictionary['subtype'])
            elif type_name == 'pointer':
                update['subtype'] = self._interdict_to_template(dictionary['subtype'])
            elif type_name == 'enum':
                update = self._lookup_enum(dictionary['name'])
            elif type_name == 'bitfield':
                update = {'start_bit': dictionary['bit_position'], 'end_bit': dictionary['bit_length']}
                update['base_type'] = self._interdict_to_template(dictionary['type'])
            native_template = native_template.clone()
            native_template.update_vol(**update)
            return native_template

        # Otherwise
        if dictionary['kind'] not in ['struct', 'union']:
            raise exceptions.SymbolSpaceError("Unknown Intermediate format: {}".format(dictionary))

        reference_name = dictionary['name']
        if constants.BANG not in reference_name:
            reference_name = self.name + constants.BANG + reference_name

        return objects.templates.ReferenceTemplate(type_name = reference_name)

    def _lookup_enum(self, name):
        """Looks up an enumeration and returns a dictionary of __init__ parameters for an Enum"""
        lookup = self._json_object['enums'].get(name, None)
        if not lookup:
            raise exceptions.SymbolSpaceError("Unknown enumeration: {}".format(name))
        result = {"choices": copy.deepcopy(lookup['constants']),
                  "base_type": self.natives.get_type(lookup['base'])}
        return result

    def get_enumeration(self, enum_name):
        """Resolves an individual enumeration"""
        if constants.BANG in enum_name:
            raise exceptions.SymbolError("Enumeration for a different table requested: {}".format(enum_name))
        if enum_name not in self._json_object['enums']:
            # Fall back to the natives table
            raise exceptions.SymbolError("Enumeration not found in {} table: {}".format(self.name, enum_name))
        curdict = self._json_object['enums'][enum_name]
        base_type = self.natives.get_type(curdict['base'])
        return objects.templates.ObjectTemplate(type_name = 'Enumeration',
                                                object_class = objects.Enumeration,
                                                base_type = base_type,
                                                size = curdict['size'],
                                                choices = curdict['constants'])

    def get_type(self, type_name):
        """Resolves an individual symbol"""
        if constants.BANG in type_name:
            raise exceptions.SymbolError("Symbol for a different table requested: {}".format(type_name))
        if type_name not in self._json_object['user_types']:
            # Fall back to the natives table
            return self.natives.get_type(self.name + constants.BANG + type_name)
        curdict = self._json_object['user_types'][type_name]
        members = {}
        for member_name in curdict['fields']:
            interdict = curdict['fields'][member_name]
            member = (interdict['offset'], self._interdict_to_template(interdict['type']))
            members[member_name] = member
        object_class = self.get_type_class(type_name)
        return objects.templates.ObjectTemplate(type_name = self.name + constants.BANG + type_name,
                                                object_class = object_class,
                                                size = curdict['length'],
                                                members = members)


class Version2Format(Version1Format):
    """Class for storing intermediate debugging data as objects and classes"""
    current = 2
    revision = 0
    age = 0
    version = (current - age, age, revision)

    def _get_natives(self):
        """Determines the appropriate native_types to use from the JSON data"""
        classes = {"x64": native.x64NativeTable, "x86": native.x86NativeTable}
        for nc in sorted(classes):
            native_class = classes[nc]
            for base_type in self._json_object['base_types']:
                try:
                    if self._json_object['base_types'][base_type]['size'] != native_class.get_type(base_type).size:
                        break
                except TypeError:
                    # TODO: determine whether we should give voids a size - We don't give voids a length, whereas microsoft seemingly do
                    pass
            else:
                vollog.debug("Choosing appropriate natives for symbol library: {}".format(nc))
                return native_class.natives

    def get_type(self, type_name):
        """Resolves an individual symbol"""
        if constants.BANG in type_name:
            raise exceptions.SymbolError("Symbol for a different table requested: {}".format(type_name))
        if type_name not in self._json_object['user_types']:
            # Fall back to the natives table
            return self.natives.get_type(self.name + constants.BANG + type_name)
        curdict = self._json_object['user_types'][type_name]
        members = {}
        for member_name in curdict['fields']:
            interdict = curdict['fields'][member_name]
            member = (interdict['offset'], self._interdict_to_template(interdict['type']))
            members[member_name] = member
        object_class = self.get_type_class(type_name)
        return objects.templates.ObjectTemplate(type_name = self.name + constants.BANG + type_name,
                                                object_class = object_class,
                                                size = curdict['size'],
                                                members = members)


class Version2_1Format(Version2Format):
    """Class for storing intermediate debugging data as objects and classes"""
    current = 2
    revision = 0
    age = 1
    version = (current - age, age, revision)

    def get_symbol(self, name):
        """Returns the location offset given by the symbol name"""
        symbol = self._json_object['symbols'].get(name, None)
        if not symbol:
            raise KeyError("Unknown symbol: {}".format(name))
        symbol_type = None
        if 'type' in symbol:
            symbol_type = self._interdict_to_template(symbol['type'])
        return interfaces.symbols.Symbol(name = name, address = symbol['address'], type = symbol_type)
