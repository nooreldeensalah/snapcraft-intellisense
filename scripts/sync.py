#!/usr/bin/env python3
# /// script
# dependencies = [
#   "beautifulsoup4==4.12.3",
# ]
# ///
"""
Sync the snapcraft.json schema by parsing official Snapcraft documentation.

This script:
1. Fetches rendered HTML documentation from Ubuntu's Snapcraft docs
2. Parses the semantic Sphinx HTML structure using BeautifulSoup
3. Extracts field definitions, types, descriptions, and enums (from "One of:" and Values tables)
4. Generates a complete JSON Schema with properly nested properties
5. Updates the local schema file

The Sphinx HTML structure is predictable:
- h3 headings contain property names (e.g., "name", "apps.<app-name>.command")
- Type/Description pairs follow headings
- "One of:" indicates inline enum values
- "Values" tables contain enum value/description pairs
- Nested properties indicated by dots in property names

Dependencies (inline - run with `uv run`):
    beautifulsoup4==4.12.3

IMPORTANT: This script has NO fallback values. If parsing fails, it will exit
with an error. This is intentional - if documentation changes, we need to update
the parser, not silently use stale data.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    print("❌ Error: BeautifulSoup4 is required. Install with: pip install beautifulsoup4")
    sys.exit(1)


# =============================================================================
# Configuration
# =============================================================================

@dataclass(frozen=True)
class DocumentationURLs:
    """URLs for schema source documentation."""

    main: str = "https://documentation.ubuntu.com/snapcraft/stable/reference/project-file/snapcraft-yaml/"
    plugins: str = "https://documentation.ubuntu.com/snapcraft/stable/reference/plugins/"
    bases: str = "https://documentation.ubuntu.com/snapcraft/stable/reference/bases/"
    extensions: str = "https://documentation.ubuntu.com/snapcraft/stable/reference/extensions/"
    interfaces: str = "https://snapcraft.io/docs/supported-interfaces"


@dataclass(frozen=True)
class ValidationThresholds:
    """Minimum expected counts for sanity checks."""

    plugins: int = 15
    bases: int = 5
    extensions: int = 4
    interfaces: int = 150
    properties: int = 50


# Only truly static values that aren't documented in tables
# Architecture names are platform/hardware constants
VALID_ARCHITECTURES = frozenset([
    "amd64", "i386", "armhf", "arm64", "ppc64el", "s390x", "riscv64"
])


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class PropertySchema:
    """Represents a parsed property schema."""

    name: str
    type_schema: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    enum_values: list[str] = field(default_factory=list)

    def to_json_schema(self) -> dict[str, Any]:
        """Convert to JSON Schema format."""
        schema: dict[str, Any] = {}

        if self.enum_values:
            schema["type"] = "string"
            schema["enum"] = self.enum_values
        elif self.type_schema:
            schema.update(self.type_schema)
        else:
            schema["type"] = "string"

        if self.description:
            schema["description"] = self.description

        return schema


@dataclass
class SchemaDefinition:
    """Represents a $defs entry for nested types."""

    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    additional_properties: bool = True

    def to_json_schema(self) -> dict[str, Any]:
        """Convert to JSON Schema format."""
        return {
            "type": "object",
            "description": self.description,
            "properties": self.properties,
            "additionalProperties": self.additional_properties,
        }


# =============================================================================
# HTTP Utilities
# =============================================================================

class HTTPClient:
    """Simple HTTP client for fetching documentation pages."""

    USER_AGENT = "Mozilla/5.0 (compatible; SnapcraftSchemaSync/2.0)"
    TIMEOUT = 30

    @classmethod
    def fetch(cls, url: str) -> str:
        """
        Fetch HTML content from URL with redirect handling.

        Args:
            url: The URL to fetch

        Returns:
            The HTML content as a string

        Raises:
            SystemExit: If fetch fails
        """
        print(f"📥 Fetching: {url}")
        try:
            opener = build_opener(HTTPRedirectHandler)
            request = Request(url, headers={"User-Agent": cls.USER_AGENT})

            with opener.open(request, timeout=cls.TIMEOUT) as response:
                final_url = response.geturl()
                if final_url != url:
                    print(f"   ↪ Redirected to: {final_url}")
                return response.read().decode("utf-8")

        except HTTPError as e:
            cls._handle_http_error(url, e)
            raise SystemExit(1) from e
        except URLError as e:
            print(f"❌ Network Error: {e.reason}\n   URL: {url}")
            raise SystemExit(1) from e
        except Exception as e:
            print(f"❌ Unexpected error: {e}\n   URL: {url}")
            raise SystemExit(1) from e

    @staticmethod
    def _handle_http_error(url: str, error: HTTPError) -> None:
        """Handle HTTP errors with helpful messages.

        Args:
            url: The URL that failed
            error: The HTTP error that occurred

        Raises:
            SystemExit: Always exits with code 1
        """
        print(f"❌ HTTP Error {error.code}: {error.reason}")
        print(f"   URL: {url}")
        print("   Documentation may have moved. Please update the URL.")


# =============================================================================
# Type Parsing
# =============================================================================

class TypeParser:
    """Parses type strings from documentation into JSON Schema types."""

    # Basic type mappings
    BASIC_TYPES: dict[str, dict[str, Any]] = {
        "str": {"type": "string"},
        "string": {"type": "string"},
        "int": {"type": "integer"},
        "integer": {"type": "integer"},
        "bool": {"type": "boolean"},
        "boolean": {"type": "boolean"},
        "float": {"type": "number"},
        "number": {"type": "number"},
        "any": {},
        "none": {"type": "null"},
        "null": {"type": "null"},
    }

    # Regex patterns
    ONE_OF_PATTERN = re.compile(r"One of:\s*\[([^\]]+)\]", re.IGNORECASE)
    DICT_PATTERN = re.compile(r"dict\[([^,]+),\s*(.+)\]", re.IGNORECASE)
    LIST_PATTERN = re.compile(r"list\[(.+)\]", re.IGNORECASE)
    SET_PATTERN = re.compile(r"set\[(.+)\]", re.IGNORECASE)

    @classmethod
    def parse(cls, type_str: str) -> dict[str, Any]:
        """
        Parse a type string into JSON Schema format.

        Handles:
        - "One of: ['value1', 'value2']" -> enum
        - Union types: "str | list[str]" -> anyOf
        - Complex types: dict[str, Any], list[str], set[str]
        - Basic types: str, int, bool, etc.
        """
        type_str = type_str.strip()

        # Handle "One of:" enum pattern
        if match := cls.ONE_OF_PATTERN.match(type_str):
            values = re.findall(r"'([^']+)'", match.group(1))
            if values:
                return {"type": "string", "enum": values}

        # Handle union types
        if "|" in type_str:
            return cls._parse_union(type_str)

        return cls._parse_single(type_str)

    @classmethod
    def _parse_union(cls, type_str: str) -> dict[str, Any]:
        """Parse union types like 'str | list[str]'."""
        parts = [p.strip() for p in type_str.split("|")]
        any_of = [cls._parse_single(p) for p in parts if cls._parse_single(p)]

        if len(any_of) == 1:
            return any_of[0]
        elif len(any_of) > 1:
            return {"anyOf": any_of}
        return {}

    @classmethod
    def _parse_single(cls, type_str: str) -> dict[str, Any]:
        """Parse a single (non-union) type."""
        type_str = type_str.strip().strip("`")

        # Dict type: dict[KeyType, ValueType]
        if match := cls.DICT_PATTERN.match(type_str):
            value_type = cls._parse_single(match.group(2).strip())
            return {
                "type": "object",
                "additionalProperties": value_type if value_type else True,
            }

        # List type: list[ItemType]
        if match := cls.LIST_PATTERN.match(type_str):
            item_type = cls._parse_single(match.group(1).strip())
            result: dict[str, Any] = {"type": "array"}
            if item_type:
                result["items"] = item_type
            return result

        # Set type: set[ItemType] -> array with uniqueItems
        if match := cls.SET_PATTERN.match(type_str):
            item_type = cls._parse_single(match.group(1).strip())
            result = {"type": "array", "uniqueItems": True}
            if item_type:
                result["items"] = item_type
            return result

        # Basic types
        type_lower = type_str.lower()
        return cls.BASIC_TYPES.get(type_lower, {})


# =============================================================================
# HTML Parsers
# =============================================================================

class PropertyExtractor:
    """Extracts property definitions from documentation HTML."""

    # Section headers to skip
    SKIP_HEADERS = frozenset({
        "top-level keys", "platform keys", "architecture keys",
        "app keys", "part keys", "socket keys", "hook keys",
        "component keys", "content plug keys", "your tracker settings",
        "additional links", "permissions keys"
    })

    # Keywords that indicate non-property headings
    SKIP_KEYWORDS = frozenset({"example", "see also", "note"})

    def __init__(self, html_content: str):
        self.soup = BeautifulSoup(html_content, "html.parser")
        self.main_content = (
            self.soup.find("main") or
            self.soup.find("article") or
            self.soup
        )

    def extract_all(self) -> dict[str, PropertySchema]:
        """Extract all property definitions from the HTML."""
        properties: dict[str, PropertySchema] = {}

        for heading in self.main_content.find_all(["h2", "h3", "h4"]):
            prop = self._extract_property(heading)
            if prop:
                properties[prop.name] = prop

        return properties

    def _extract_property(self, heading: Tag) -> PropertySchema | None:
        """Extract a single property definition from a heading element."""
        heading_text = heading.get_text(strip=True).replace("¶", "").strip()

        # Skip invalid headings
        if not heading_text or heading_text.lower() in self.SKIP_HEADERS:
            return None
        if any(kw in heading_text.lower() for kw in self.SKIP_KEYWORDS):
            return None

        prop = PropertySchema(name=heading_text)
        self._parse_property_content(heading, prop)

        return prop

    def _parse_property_content(self, heading: Tag, prop: PropertySchema) -> None:
        """Parse the content following a property heading."""
        current = heading.find_next_sibling()
        expecting_type = False
        expecting_desc = False
        found_type = False
        found_desc = False

        while current:
            # Stop at next heading
            if current.name in ("h2", "h3", "h4"):
                break

            if current.name == "p":
                # Check for labels first
                strong = current.find("strong")
                if strong:
                    label = strong.get_text(strip=True).lower()
                    if label == "type":
                        expecting_type, expecting_desc = True, False
                    elif label == "description":
                        expecting_type, expecting_desc = False, True
                        found_type = True  # Type section ended
                    elif label == "values":
                        expecting_type, expecting_desc = False, False
                else:
                    # Process content based on current state
                    text = current.get_text(strip=True)

                    if expecting_type and not found_type:
                        # Extract type value
                        if text.lower().startswith("one of:"):
                            type_text = text
                        else:
                            codes = current.find_all("code")
                            type_text = " ".join(
                                c.get_text(strip=True) for c in codes if c.get_text(strip=True)
                            ) if codes else text

                        if type_text:
                            prop.type_schema = TypeParser.parse(type_text)
                            found_type = True
                            expecting_type = False

                    elif expecting_desc and not found_desc:
                        # Extract description value
                        desc = text[:497] + "..." if len(text) > 500 else text
                        if desc:
                            prop.description = desc
                            found_desc = True
                            expecting_desc = False

            elif current.name == "table":
                # Parse Values table for enum values
                enum_values = self._extract_table_values(current)
                if enum_values and not prop.enum_values:
                    prop.enum_values = enum_values

            elif current.name == "div":
                # Tables may be wrapped in div.table-wrapper containers
                table = current.find("table")
                if table:
                    enum_values = self._extract_table_values(table)
                    if enum_values and not prop.enum_values:
                        prop.enum_values = enum_values

            elif current.name == "dl":
                # Fallback for definition list format
                self._process_definition_list(current, prop)

            current = current.find_next_sibling()

        # Ensure type is set
        if not prop.type_schema and not prop.enum_values:
            prop.type_schema = {"type": "string"}

    def _extract_table_values(self, table: Tag) -> list[str]:
        """Extract enum values from a Values table."""
        values = []
        skip_values = {"value", "values", "name", ""}

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if cells:
                value = cells[0].get_text(strip=True)
                if value and value.lower() not in skip_values:
                    # Clean up the value - take first word if contains space
                    # Some tables have "value Description" format
                    clean_value = value.split()[0] if " " in value else value
                    if clean_value not in values:
                        values.append(clean_value)

        return sorted(values)

    def _process_definition_list(self, dl: Tag, prop: PropertySchema) -> None:
        """Process a definition list element (fallback format)."""
        for dt in dl.find_all("dt", recursive=False):
            label = dt.get_text(strip=True).lower()
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue

            if label == "type" and not prop.type_schema:
                codes = dd.find_all("code")
                type_text = (
                    " ".join(c.get_text(strip=True) for c in codes if c.get_text(strip=True))
                    if codes else dd.get_text(strip=True)
                )
                if type_text:
                    prop.type_schema = TypeParser.parse(type_text)

            elif label == "description" and not prop.description:
                desc = re.sub(r"\s+", " ", dd.get_text(separator=" ", strip=True))
                prop.description = desc[:497] + "..." if len(desc) > 500 else desc


class PluginParser:
    """Parses plugin names from the plugins documentation page."""

    # Pattern to extract plugin name from URL like "flutter_plugin/" or "dotnet_v2_plugin/"
    # Handles both absolute and relative URLs
    PLUGIN_URL_PATTERN = re.compile(r"(?:^|/)([a-z0-9_]+)_plugin/?$", re.IGNORECASE)

    @classmethod
    def parse(cls, html_content: str, min_expected: int) -> list[str]:
        """Parse and validate plugin names from URLs."""
        soup = BeautifulSoup(html_content, "html.parser")
        plugins = set()

        for link in soup.find_all("a", href=True):
            href = link["href"]
            # Extract plugin name from URL (e.g., /flutter_plugin/ -> flutter)
            if match := cls.PLUGIN_URL_PATTERN.search(href):
                raw_name = match.group(1).lower()
                # Convert underscores to hyphens and clean up
                # Handle special cases like "dotnet_v2" -> "dotnet-v2", "go_use" -> "go-use"
                name = raw_name.replace("_", "-")
                # Remove trailing version indicators for cleaner names if needed
                if name and len(name) < 30:
                    plugins.add(name)

        result = sorted(plugins)
        if len(result) < min_expected:
            raise ValueError(
                f"Parsed {len(result)} plugins, expected at least {min_expected}. "
                "Documentation structure may have changed."
            )
        return result


class BaseParser:
    """Parses base snap names from the bases documentation page."""

    BASE_PATTERN = re.compile(r"^(core\d*|bare|devel)$")

    @classmethod
    def parse(cls, html_content: str, min_expected: int) -> list[str]:
        """Parse and validate base names."""
        soup = BeautifulSoup(html_content, "html.parser")
        bases = set()

        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if cells:
                    text = cells[0].get_text(strip=True)
                    if cls.BASE_PATTERN.match(text):
                        bases.add(text)

        result = sorted(bases)
        if len(result) < min_expected:
            raise ValueError(
                f"Parsed {len(result)} bases, expected at least {min_expected}. "
                "Documentation structure may have changed."
            )
        return result


class ExtensionParser:
    """Fetches extension names from canonical/snapcraft registry.py (source of truth)."""

    # GitHub raw URLs for the registry files
    REGISTRY_URL = "https://raw.githubusercontent.com/canonical/snapcraft/main/snapcraft/extensions/registry.py"
    LEGACY_SCHEMA_URL = "https://raw.githubusercontent.com/canonical/snapcraft/main/schema/snapcraft-legacy.json"

    @classmethod
    def parse(cls, html_content: str, min_expected: int) -> list[str]:
        """Fetch extension names from canonical/snapcraft registry files.

        Fetches from two sources:
        1. Modern extensions (core22+): registry.py
        2. Legacy extensions (core18/core20): snapcraft-legacy.json

        Args:
            html_content: Unused (kept for API compatibility)
            min_expected: Minimum number of extensions expected

        Returns:
            Sorted list of extension names
        """
        extensions = set()
        modern_count = 0
        legacy_count = 0

        # Fetch modern extensions from registry.py
        try:
            print("  Fetching modern extensions from registry.py")
            registry_content = HTTPClient.fetch(cls.REGISTRY_URL)

            # Parse the _EXTENSIONS dictionary using regex
            # Pattern: "extension-name": SomeExtensionClass,
            extension_pattern = re.compile(r'"([a-z0-9-]+)"\s*:', re.MULTILINE)
            modern_extensions = set(extension_pattern.findall(registry_content))
            extensions.update(modern_extensions)
            modern_count = len(modern_extensions)
            print(f"  Found {modern_count} modern extensions")
        except (HTTPError, URLError, OSError) as e:
            print(f"  ⚠️  Failed to fetch modern extensions: {e}")

        # Fetch legacy extensions from snapcraft-legacy.json
        try:
            print("  Fetching legacy extensions from snapcraft-legacy.json")
            legacy_schema = HTTPClient.fetch(cls.LEGACY_SCHEMA_URL)
            legacy_data = json.loads(legacy_schema)

            # Find extensions enum in the schema
            legacy_extensions = cls._extract_legacy_extensions(legacy_data)
            if legacy_extensions:
                extensions.update(legacy_extensions)
                legacy_count = len(legacy_extensions)
                print(f"  Found {legacy_count} legacy extensions")
            else:
                print("  ⚠️  No extensions found in legacy schema")
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
            print(f"  ⚠️  Failed to fetch legacy extensions: {e}")

        result = sorted(extensions)
        if len(result) < min_expected:
            raise ValueError(
                f"Parsed {len(result)} extensions total (modern: {modern_count}, legacy: {legacy_count}), "
                f"expected at least {min_expected}. Repository structure may have changed."
            )

        print(f"  ✅ Total: {len(result)} extensions (modern: {modern_count}, legacy: {legacy_count})")
        return result

    @classmethod
    def _extract_legacy_extensions(cls, schema: dict | list, path: str = '') -> set[str]:
        """Recursively extract extension names from legacy schema's enum values.

        Args:
            schema: The JSON schema dictionary or list to search
            path: Current path in the schema (for 'extension' keyword detection)

        Returns:
            Set of extension names found in enum values
        """
        extensions: set[str] = set()

        if isinstance(schema, dict):
            # Check if this is an extensions enum
            if 'enum' in schema and 'extension' in path.lower():
                extensions.update(schema['enum'])
            # Recursively search through the schema
            for key, value in schema.items():
                extensions.update(cls._extract_legacy_extensions(value, f'{path}.{key}'))
        elif isinstance(schema, list):
            for item in schema:
                extensions.update(cls._extract_legacy_extensions(item, path))

        return extensions


class InterfaceParser:
    """Parses interface names from the supported interfaces page."""

    SKIP_VALUES = frozenset({"interface", "name", ""})

    @classmethod
    def parse(cls, html_content: str, min_expected: int) -> list[str]:
        """Parse and validate interface names."""
        soup = BeautifulSoup(html_content, "html.parser")
        interfaces = set()

        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    name = cells[0].get_text(strip=True)
                    if name and name.lower() not in cls.SKIP_VALUES:
                        interfaces.add(name.strip())

        result = sorted(interfaces)
        if len(result) < min_expected:
            raise ValueError(
                f"Parsed {len(result)} interfaces, expected at least {min_expected}. "
                "Documentation structure may have changed."
            )
        return result


# =============================================================================
# Schema Builder
# =============================================================================

class SchemaBuilder:
    """Builds JSON Schema from extracted properties."""

    # Property path prefixes for categorization
    PREFIX_MAPPINGS = [
        ("apps.<app-name>.sockets.<socket-name>.", "sockets"),
        ("sockets.<socket-name>.", "sockets"),
        ("apps.<app-name>.", "apps"),
        ("parts.<part-name>.permissions.<permission>.", "permissions"),
        ("parts.<part-name>.", "parts"),
        ("platforms.<platform-name>.", "platforms"),
        ("architectures.<architecture>.", "architectures"),
        ("hooks.<hook-type>.", "hooks"),
        ("components.<component-name>.hooks.<hook-type>.", None),  # Skip
        ("components.<component-name>.", "components"),
        ("plugs.<plug-name>.", "plugs"),
        ("slots.<slot-name>.", "slots"),
        ("lint.", "lint"),
    ]

    def __init__(self, properties: dict[str, PropertySchema], docs_url: str):
        self.properties = properties
        self.docs_url = docs_url
        self.categorized: dict[str, dict[str, dict[str, Any]]] = {
            "top_level": {},
            "apps": {},
            "parts": {},
            "platforms": {},
            "architectures": {},
            "sockets": {},
            "hooks": {},
            "components": {},
            "plugs": {},
            "slots": {},
            "permissions": {},
            "lint": {},
        }

    def build(self) -> dict[str, Any]:
        """Build the complete JSON Schema."""
        self._categorize_properties()
        defs = self._build_definitions()
        top_level = self._build_top_level(defs)

        schema: dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://raw.githubusercontent.com/nooreldeensalah/snapcraft-intellisense/main/schemas/snapcraft.json",
            "title": "Snapcraft YAML Schema",
            "description": f"Schema for snapcraft.yaml. Auto-generated from: {self.docs_url}",
            "type": "object",
            "properties": top_level,
            "required": ["name"],
            "additionalProperties": True,
        }

        if defs:
            schema["$defs"] = defs

        return schema

    def _categorize_properties(self) -> None:
        """Categorize properties by their path prefix."""
        for path, prop in self.properties.items():
            category = self._get_category(path)
            if category:
                name = self._strip_prefix(path)
                # Skip doubly nested properties (except for specific cases)
                if ".<" not in name:
                    self.categorized[category][name] = prop.to_json_schema()

    def _get_category(self, path: str) -> str | None:
        """Determine the category for a property path."""
        for prefix, category in self.PREFIX_MAPPINGS:
            if path.startswith(prefix):
                return category

        # Top-level if no nested placeholder
        if ".<" not in path and "<" not in path:
            return "top_level"
        return None

    def _strip_prefix(self, path: str) -> str:
        """Remove the prefix from a property path."""
        for prefix, _ in self.PREFIX_MAPPINGS:
            if path.startswith(prefix):
                return path[len(prefix):]
        return path

    def _build_definitions(self) -> dict[str, dict[str, Any]]:
        """Build the $defs section."""
        defs: dict[str, dict[str, Any]] = {}

        definitions = [
            ("Socket", "sockets", "Socket configuration for app activation", False),
            ("Hook", "hooks", "Hook configuration", False),
            ("Permissions", "permissions", "File permission settings", False),
            ("Lint", "lint", "Linting configuration", False),
            ("Platform", "platforms", "Platform/architecture configuration", False),
            ("Architecture", "architectures", "Architecture configuration", False),
            ("ContentPlug", "plugs", "Content interface plug definition", True),
        ]

        for name, category, description, allow_additional in definitions:
            if self.categorized[category]:
                defs[name] = SchemaDefinition(
                    name=name,
                    description=description,
                    properties=self.categorized[category],
                    additional_properties=allow_additional,
                ).to_json_schema()

        # Special handling for Component (needs hooks reference)
        if self.categorized["components"]:
            comp_props = self.categorized["components"].copy()
            if "Hook" in defs:
                comp_props["hooks"] = {
                    "type": "object",
                    "description": "Component lifecycle hooks",
                    "additionalProperties": {"$ref": "#/$defs/Hook"},
                }
            defs["Component"] = SchemaDefinition(
                name="Component",
                description="Snap component definition",
                properties=comp_props,
                additional_properties=False,
            ).to_json_schema()

        # App definition (needs sockets reference)
        if self.categorized["apps"]:
            app_props = self.categorized["apps"].copy()
            if "Socket" in defs:
                app_props["sockets"] = {
                    "type": "object",
                    "description": "Socket activation configuration",
                    "additionalProperties": {"$ref": "#/$defs/Socket"},
                }
            defs["App"] = SchemaDefinition(
                name="App",
                description="Application definition",
                properties=app_props,
                additional_properties=False,
            ).to_json_schema()

        # Part definition (needs permissions reference)
        if self.categorized["parts"]:
            part_props = self.categorized["parts"].copy()
            if "Permissions" in defs:
                part_props["permissions"] = {
                    "type": "array",
                    "description": "File permission settings",
                    "items": {"$ref": "#/$defs/Permissions"},
                }
            defs["Part"] = SchemaDefinition(
                name="Part",
                description="Part definition for building snap components",
                properties=part_props,
                additional_properties=True,  # Allow plugin-specific properties
            ).to_json_schema()

        return defs

    def _build_top_level(self, defs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Build top-level properties with references to definitions."""
        top_level = self.categorized["top_level"].copy()

        # Map top-level keys to their definitions
        ref_mappings = [
            ("apps", "App", "Application definitions"),
            ("parts", "Part", "Part definitions for building the snap"),
            ("hooks", "Hook", "Lifecycle hooks"),
            ("components", "Component", "Snap components"),
        ]

        for key, def_name, description in ref_mappings:
            if def_name in defs or self.categorized.get(key.rstrip("s"), {}):
                top_level[key] = {
                    "type": "object",
                    "description": description,
                    "additionalProperties": (
                        {"$ref": f"#/$defs/{def_name}"} if def_name in defs
                        else {"type": "object", "additionalProperties": True}
                    ),
                }

        # Platforms with null option for shorthand
        if "Platform" in defs or self.categorized["platforms"]:
            top_level["platforms"] = {
                "type": "object",
                "description": "Platform/architecture configurations",
                "additionalProperties": {
                    "anyOf": [
                        {"$ref": "#/$defs/Platform"} if "Platform" in defs else {"type": "object"},
                        {"type": "null"},  # Allow shorthand like "amd64:"
                    ]
                },
            }

        # Architectures (array with string or object)
        if "Architecture" in defs or self.categorized["architectures"]:
            top_level["architectures"] = {
                "type": "array",
                "description": "Architecture configurations (for core22 and older)",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {"$ref": "#/$defs/Architecture"} if "Architecture" in defs else {"type": "object"},
                    ]
                },
            }

        # Plugs and slots
        for key in ["plugs", "slots"]:
            if key not in top_level:
                top_level[key] = {
                    "type": "object",
                    "description": f"Interface {key}",
                    "additionalProperties": True,
                }

        # Lint reference
        if "Lint" in defs:
            top_level["lint"] = {"$ref": "#/$defs/Lint"}
        elif "lint" not in top_level:
            top_level["lint"] = {"type": "object", "additionalProperties": True}

        return top_level


# =============================================================================
# Schema Enhancer
# =============================================================================

class SchemaEnhancer:
    """Enhances schema with dynamically parsed enum values."""

    def __init__(
        self,
        schema: dict[str, Any],
        plugins: list[str],
        bases: list[str],
        extensions: list[str],
        interfaces: list[str],
    ):
        self.schema = schema
        self.plugins = plugins
        self.bases = bases
        self.extensions = extensions
        self.interfaces = interfaces

    def enhance(self) -> dict[str, Any]:
        """Apply all enhancements to the schema."""
        print("\n🎨 Enhancing schema with parsed enum values...")

        self._enhance_plugins()
        self._enhance_bases()
        self._enhance_extensions()
        self._enhance_interfaces()
        self._enhance_architectures()

        print("✅ Schema enhancement complete!\n")
        return self.schema

    def _enhance_plugins(self) -> None:
        """Add plugin enum to Part definition."""
        if not self.plugins:
            return

        part_def = self.schema.get("$defs", {}).get("Part", {})
        if "plugin" in part_def.get("properties", {}):
            part_def["properties"]["plugin"]["enum"] = self.plugins
            print(f"  ✓ Added {len(self.plugins)} plugin names")

    def _enhance_bases(self) -> None:
        """Add base enums to base and build-base properties."""
        if not self.bases:
            return

        props = self.schema.get("properties", {})

        if "base" in props:
            props["base"]["enum"] = self.bases
            print(f"  ✓ Added {len(self.bases)} base snap names")

        if "build-base" in props:
            build_bases = self.bases + (["devel"] if "devel" not in self.bases else [])
            props["build-base"]["enum"] = build_bases

    def _enhance_extensions(self) -> None:
        """Add extension enum to App definition."""
        if not self.extensions:
            return

        app_def = self.schema.get("$defs", {}).get("App", {})
        app_props = app_def.get("properties", {})

        if "extensions" in app_props:
            ext_schema = app_props["extensions"]
            if ext_schema.get("type") == "array":
                ext_schema["items"] = {"type": "string", "enum": self.extensions}
            else:
                ext_schema["enum"] = self.extensions
            print(f"  ✓ Added {len(self.extensions)} extension names")

    def _enhance_interfaces(self) -> None:
        """Add interface definitions to plugs and slots.

        Plugs and slots use custom names as property keys (e.g., 'dbus-svc', 'foo-plug').
        The 'interface' property inside each plug/slot specifies the interface type.
        """
        if not self.interfaces:
            return

        print(f"  ✓ Added {len(self.interfaces)} interface names")

        props = self.schema.get("properties", {})

        # Define the schema for plug/slot definitions (allows custom property names)
        # The interface type is specified via the 'interface' property inside
        plug_slot_value_schema = {
            "anyOf": [
                {"type": "null"},  # Allow shorthand: "desktop:" with no value
                {"type": "string"},  # Allow shorthand: "content: $SNAP/shared"
                {
                    "type": "object",
                    "properties": {
                        "interface": {
                            "type": "string",
                            "enum": self.interfaces,
                            "description": "The interface type for this plug/slot."
                        },
                        "bus": {
                            "type": "string",
                            "enum": ["session", "system"],
                            "description": "D-Bus bus type (for dbus interface)."
                        },
                        "name": {
                            "type": "string",
                            "description": "Well-known D-Bus name or content tag."
                        },
                        "target": {
                            "type": "string",
                            "description": "Target path (for content interface)."
                        },
                        "default-provider": {
                            "type": "string",
                            "description": "Default content provider snap."
                        },
                        "content": {
                            "type": "string",
                            "description": "Content tag identifier."
                        }
                    },
                    "additionalProperties": True
                }
            ]
        }

        # Top-level plugs/slots allow custom names as keys (no propertyNames restriction)
        for key in ["plugs", "slots"]:
            if key in props:
                props[key] = {
                    "type": "object",
                    "description": f"Declares the snap's {key}. Property names are custom identifiers.\n\nAvailable interfaces: {', '.join(self.interfaces[:25])}...",
                    "additionalProperties": plug_slot_value_schema
                }

        # App-level plugs/slots are arrays of interface names (strings)
        app_def = self.schema.get("$defs", {}).get("App", {})
        app_props = app_def.get("properties", {})
        if "plugs" in app_props and app_props["plugs"].get("type") == "array":
            app_props["plugs"]["items"] = {"type": "string", "enum": self.interfaces}
        if "slots" in app_props and app_props["slots"].get("type") == "array":
            app_props["slots"]["items"] = {"type": "string", "enum": self.interfaces}

    def _enhance_architectures(self) -> None:
        """Add architecture enum to relevant fields."""
        archs = sorted(VALID_ARCHITECTURES)
        props = self.schema.get("properties", {})

        # Architectures array items
        if "architectures" in props:
            arch_def = props["architectures"]
            if "items" in arch_def and "anyOf" in arch_def["items"]:
                for item in arch_def["items"]["anyOf"]:
                    if item.get("type") == "string":
                        item["enum"] = archs
                        print(f"  ✓ Added {len(archs)} architecture names")
                        break

        # Platform property names
        if "platforms" in props:
            props["platforms"]["propertyNames"] = {"enum": archs}


# =============================================================================
# Main Entry Point
# =============================================================================

def main() -> int:
    """
    Main entry point - NO FALLBACKS, FAIL FAST!

    If this script fails, it means the documentation structure has changed.
    Update the parsing logic to match the new structure.
    """
    print("=" * 60)
    print("Snapcraft Schema Sync Tool v2.0")
    print("Parsing from official documentation - NO FALLBACKS")
    print("=" * 60)

    urls = DocumentationURLs()
    thresholds = ValidationThresholds()

    # Paths
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    schema_output = project_root / "schemas" / "snapcraft.json"

    # Fetch and parse main documentation
    html_content = HTTPClient.fetch(urls.main)
    print(f"✓ Fetched {len(html_content)} bytes of HTML\n")

    extractor = PropertyExtractor(html_content)
    properties = extractor.extract_all()
    print(f"✓ Extracted {len(properties)} property definitions\n")

    if len(properties) < thresholds.properties:
        print(f"❌ Only extracted {len(properties)} properties, expected at least {thresholds.properties}")
        sys.exit(1)

    # Build initial schema
    builder = SchemaBuilder(properties, urls.main)
    schema = builder.build()

    prop_count = len(schema.get("properties", {}))
    defs_count = len(schema.get("$defs", {}))
    print(f"✓ Generated schema: {prop_count} top-level properties, {defs_count} definitions\n")

    # Fetch dynamic enum values
    print("=" * 60)
    print("Fetching dynamic enum values from documentation...")
    print("=" * 60 + "\n")

    plugins_html = HTTPClient.fetch(urls.plugins)
    plugins = PluginParser.parse(plugins_html, thresholds.plugins)
    print(f"✅ Parsed {len(plugins)} plugins\n")

    bases_html = HTTPClient.fetch(urls.bases)
    bases = BaseParser.parse(bases_html, thresholds.bases)
    print(f"✅ Parsed {len(bases)} bases\n")

    extensions_html = HTTPClient.fetch(urls.extensions)
    extensions = ExtensionParser.parse(extensions_html, thresholds.extensions)
    print(f"✅ Parsed {len(extensions)} extensions\n")

    interfaces_html = HTTPClient.fetch(urls.interfaces)
    interfaces = InterfaceParser.parse(interfaces_html, thresholds.interfaces)
    print(f"✅ Parsed {len(interfaces)} interfaces\n")

    # Enhance schema with dynamic values
    enhancer = SchemaEnhancer(schema, plugins, bases, extensions, interfaces)
    schema = enhancer.enhance()

    # Write schema
    new_content = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"

    if schema_output.exists():
        current_content = schema_output.read_text(encoding="utf-8")
        if current_content == new_content:
            print("💚 Schema is already up to date. No changes needed.")
            return 0
        print("🔄 Schema has changed. Updating...")

    print(f"Writing schema to {schema_output}")
    schema_output.parent.mkdir(parents=True, exist_ok=True)
    schema_output.write_text(new_content, encoding="utf-8", newline="\n")
    print("✅ Schema updated successfully!")

    # Summary
    print("\nSchema Summary:")
    print(f"Top-level properties: {prop_count}")
    print(f"Definitions ($defs): {defs_count}")
    print(
        f"Dynamic enums: plugins({len(plugins)}), bases({len(bases)}), "
        f"extensions({len(extensions)}), interfaces({len(interfaces)})"
    )

    if properties := schema.get("properties"):
        sample = list(properties.keys())[:10]
        print(f"Sample properties: {', '.join(sample)}")
    if defs := schema.get("$defs"):
        print(f"Definitions: {', '.join(defs.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
