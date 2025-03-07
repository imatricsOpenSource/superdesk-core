# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license


import logging
import json
from typing import List, Any, Dict, Optional

from flask import request, current_app as app
from eve.utils import config
from eve.methods.common import serialize_value
from flask_babel import _, lazy_gettext

from superdesk import privilege, get_resource_service
from superdesk.notification import push_notification
from superdesk.resource import Resource
from superdesk.services import BaseService
from superdesk.users import get_user_from_request
from superdesk.utc import utcnow
from superdesk.errors import SuperdeskApiError
from superdesk.default_schema import DEFAULT_SCHEMA, DEFAULT_EDITOR
from copy import deepcopy

logger = logging.getLogger(__name__)

KEYWORDS_CV = "keywords"


privilege(
    name="vocabularies",
    label=lazy_gettext("Vocabularies Management"),
    description=lazy_gettext("User can manage vocabularies' contents."),
)


# TODO(petr): add api to specify vocabulary schema
vocab_schema = {
    "crop_sizes": {
        "width": {"type": "integer"},
        "height": {"type": "integer"},
    }
}


class VocabulariesResource(Resource):
    schema = {
        "_id": {
            "type": "string",
            "unique": True,
            "required": True,
            "regex": "^[a-zA-Z0-9-_]+$",
        },
        "display_name": {"type": "string", "required": True},
        "description": {"type": "string", "required": False},
        "helper_text": {"type": "string", "maxlength": 120},
        "tags": {
            "type": "list",
            "required": False,
            "schema": {
                "type": "dict",
                "schema": {"text": {"type": "string"}},
            },
        },
        "popup_width": {"type": "integer", "nullable": True},
        "type": {
            "type": "string",
            "required": True,
            "allowed": ["manageable", "unmanageable"],
        },
        "items": {
            "type": "list",
            "required": True,
            "schema": {
                "type": "dict",
                "allow_unknown": True,
                "schema": {
                    "name": {"type": "string", "required": False, "nullable": True},
                },
            },
        },
        "selection_type": {
            "type": "string",
            "allowed": ["single selection", "multi selection", "do not show"],
            "nullable": True,
        },
        "read_only": {"type": "boolean", "required": False, "nullable": True},
        "schema_field": {"type": "string", "required": False, "nullable": True},
        "dependent": {
            "type": "boolean",
        },
        "service": {
            "type": "dict",
            "schema": {},
            "allow_unknown": True,
            "keysrules": {"type": "string"},
            "valuesrules": {"type": "integer"},
        },
        "priority": {"type": "integer"},
        "unique_field": {"type": "string", "required": False, "nullable": True},
        "schema": {
            "type": "dict",
            "schema": {},
            "allow_unknown": True,
        },
        "field_type": {
            "type": "string",
            "nullable": True,
        },
        "field_options": {
            "type": "dict",
            "schema": {},
            "allow_unknown": True,
        },
        "init_version": {
            "type": "integer",
        },
        "preffered_items": {
            "type": "boolean",
        },
        "disable_entire_category_selection": {"type": "boolean", "default": False},
        "date_shortcuts": {
            "type": "list",
            "nullable": True,
            "schema": {
                "type": "dict",
                "schema": {
                    "value": {"type": "integer", "required": True},
                    "term": {"type": "string", "required": True},
                    "label": {"type": "string", "required": True},
                },
            },
        },
        "custom_field_type": {
            "type": "string",
            "nullable": True,
        },
        "custom_field_config": {
            "type": "dict",
            "nullable": True,
            "schema": {
                "increment_steps": {
                    "type": "list",
                },
                "initial_offset_minutes": {
                    "type": "integer",
                },
            },
            "allow_unknown": True,
        },
        "translations": {
            "type": "dict",
            "schema": {
                "display_name": {
                    "type": "dict",
                },
            },
        },
    }

    soft_delete = True
    item_url = r'regex("[-_\w]+")'
    item_methods = ["GET", "PATCH", "DELETE"]
    resource_methods = ["GET", "POST"]
    privileges = {"PATCH": "vocabularies", "POST": "vocabularies", "DELETE": "vocabularies"}
    mongo_indexes = {"field_type": [("field_type", 1)]}


class VocabulariesService(BaseService):

    system_keys = set(DEFAULT_SCHEMA.keys()).union(set(DEFAULT_EDITOR.keys()))

    def _validate_items(self, update):
        # if we have qcode and not unique_field set, we want it to be qcode
        try:
            update["schema"]["qcode"]
        except KeyError:
            pass
        else:
            update.setdefault("unique_field", "qcode")
        unique_field = update.get("unique_field")
        vocabs = {}
        if "schema" in update and "items" in update:
            for index, item in enumerate(update["items"]):
                for field, desc in update.get("schema", {}).items():
                    if (desc.get("required", False) or unique_field == field) and (
                        field not in item or not item[field]
                    ):
                        msg = "Required " + field + " in item " + str(index)
                        payload = {"error": {"required_field": 1}, "params": {"field": field, "item": index}}
                        raise SuperdeskApiError.badRequestError(message=msg, payload=payload)

                    elif desc.get("link_vocab") and desc.get("link_field"):
                        if not vocabs.get(desc["link_vocab"]):
                            linked_vocab = self.find_one(req=None, _id=desc["link_vocab"]) or {}

                            vocabs[desc["link_vocab"]] = [
                                vocab.get(desc["link_field"]) for vocab in linked_vocab.get("items") or []
                            ]

                        if item.get(field) and item[field] not in vocabs[desc["link_vocab"]]:
                            msg = '{} "{}={}" not found'.format(desc["link_vocab"], desc["link_field"], item[field])
                            payload = {"error": {"required_field": 1, "params": {"field": field, "item": index}}}
                            raise SuperdeskApiError.badRequestError(message=msg, payload=payload)

    def on_create(self, docs):
        for doc in docs:
            self._validate_items(doc)

            if doc.get("field_type") and doc["_id"] in self.system_keys:
                raise SuperdeskApiError(message="{} is in use".format(doc["_id"]), payload={"_id": {"conflict": 1}})

            if self.find_one(req=None, **{"_id": doc["_id"], "_deleted": True}):
                raise SuperdeskApiError(
                    message="{} is used by deleted vocabulary".format(doc["_id"]), payload={"_id": {"deleted": 1}}
                )

    def on_created(self, docs):
        for doc in docs:
            self._send_notification(doc, event="vocabularies:created")

    def on_replace(self, document, original):
        self._validate_items(document)
        document[app.config["LAST_UPDATED"]] = utcnow()
        document[app.config["DATE_CREATED"]] = (
            original.get(app.config["DATE_CREATED"], utcnow()) if original else utcnow()
        )
        logger.info("updating vocabulary item: %s", document["_id"])

    def on_fetched(self, doc):
        """Overriding to filter out inactive vocabularies and pops out 'is_active' property from the response.

        It keeps it when requested for manageable vocabularies.
        """

        if request and hasattr(request, "args") and request.args.get("where"):
            where_clause = json.loads(request.args.get("where"))
            if where_clause.get("type") == "manageable":
                return doc

        for item in doc[config.ITEMS]:
            self._filter_inactive_vocabularies(item)
            self._cast_items(item)

    def on_fetched_item(self, doc):
        """
        Overriding to filter out inactive vocabularies and pops out 'is_active' property from the response.
        """
        self._filter_inactive_vocabularies(doc)
        self._cast_items(doc)

    def on_update(self, updates, original):
        """Checks the duplicates if a unique field is defined"""
        if "items" in updates:
            updated = deepcopy(original)
            updated.update(updates)
            self._validate_items(updated)
        unique_field = original.get("unique_field")
        if unique_field:
            self._check_uniqueness(updates.get("items", []), unique_field)

    def on_updated(self, updates, original):
        """
        Overriding this to send notification about the replacement
        """
        self._send_notification(original)

    def on_replaced(self, document, original):
        """
        Overriding this to send notification about the replacement
        """
        self._send_notification(document)

    def on_delete(self, doc):
        """
        Overriding to validate vocabulary deletion
        """
        if "field_type" not in doc:
            raise SuperdeskApiError.badRequestError("Default vocabularies cannot be deleted")

    def _check_uniqueness(self, items, unique_field):
        """Checks the uniqueness if a unique field is defined

        :param items: list of items to check for uniqueness
        :param unique_field: name of the unique field
        """
        unique_values = []
        for item in items:
            # compare only the active items
            if not item.get("is_active"):
                continue

            if not item.get(unique_field):
                raise SuperdeskApiError.badRequestError("{} cannot be empty".format(unique_field))

            unique_value = str(item.get(unique_field)).upper()

            if unique_value in unique_values:
                raise SuperdeskApiError.badRequestError(
                    "Value {} for field {} is not unique".format(item.get(unique_field), unique_field)
                )

            unique_values.append(unique_value)

    def _filter_inactive_vocabularies(self, item):
        vocs = item["items"]
        active_vocs = (
            {k: voc[k] for k in voc.keys() if k != "is_active"} for voc in vocs if voc.get("is_active", True)
        )

        item["items"] = list(active_vocs)

    def _cast_items(self, vocab):
        """Cast values in vocabulary items using predefined schema.

        :param vocab
        """
        schema = vocab_schema.get(vocab.get("_id"), {})
        for item in vocab.get("items", []):
            for field, field_schema in schema.items():
                if field in item:
                    item[field] = serialize_value(field_schema["type"], item[field])

    def _send_notification(self, updated_vocabulary, event="vocabularies:updated"):
        """
        Sends notification about the updated vocabulary to all the connected clients.
        """

        user = get_user_from_request()
        push_notification(
            event,
            vocabulary=updated_vocabulary.get("display_name"),
            user=str(user[config.ID_FIELD]) if user else None,
            vocabulary_id=updated_vocabulary["_id"],
        )

    def get_rightsinfo(self, item):
        rights_key = item.get("source", item.get("original_source", "default"))
        all_rights = self.find_one(req=None, _id="rightsinfo")
        if not all_rights or not all_rights.get("items"):
            return {}
        try:
            all_rights["items"] = self.get_locale_vocabulary(all_rights.get("items"), item.get("language"))
            default_rights = next(info for info in all_rights["items"] if info["name"] == "default")
        except StopIteration:
            default_rights = None
        try:
            rights = next(info for info in all_rights["items"] if info["name"] == rights_key)
        except StopIteration:
            rights = default_rights
        if rights:
            return {
                "copyrightholder": rights.get("copyrightHolder"),
                "copyrightnotice": rights.get("copyrightNotice"),
                "usageterms": rights.get("usageTerms"),
            }
        else:
            return {}

    def get_extra_fields(self):
        return list(self.get(req=None, lookup={"field_type": {"$exists": True, "$ne": None}}))

    def get_custom_vocabularies(self):
        return list(
            self.get(
                req=None,
                lookup={
                    "field_type": None,
                    "service": {"$exists": True},
                },
            )
        )

    def get_forbiden_custom_vocabularies(self):
        return list(
            self.get(
                req=None,
                lookup={
                    "field_type": None,
                    "selection_type": "do not show",
                    "service": {"$exists": True},
                },
            )
        )

    def get_locale_vocabulary(self, vocabulary, language):
        if not vocabulary or not language:
            return vocabulary
        locale_vocabulary = []
        for item in vocabulary:
            if "translations" not in item:
                locale_vocabulary.append(item)
                continue
            new_item = item.copy()
            locale_vocabulary.append(new_item)
            for field, values in new_item.get("translations", {}).items():
                if field in new_item and language in values:
                    new_item[field] = values[language]
        return locale_vocabulary

    def add_missing_keywords(self, keywords, language=None):
        if not keywords:
            return
        cv = self.find_one(req=None, _id=KEYWORDS_CV)
        if cv:
            existing = {item["name"].lower() for item in cv.get("items", [])}
            missing = [keyword for keyword in keywords if keyword.lower() not in existing]
            if missing:
                updates = {"items": cv.get("items", [])}
                for keyword in missing:
                    updates["items"].append(
                        {
                            "name": keyword,
                            "qcode": keyword,
                            "is_active": True,
                        }
                    )
                self.on_update(updates, cv)
                self.system_update(cv["_id"], updates, cv)
                self.on_updated(updates, cv)
        else:
            items = [
                {
                    "name": keyword,
                    "qcode": keyword,
                    "is_active": True,
                }
                for keyword in keywords
            ]
            cv = {
                "_id": KEYWORDS_CV,
                "items": items,
                "type": "manageable",
                "display_name": _("Keywords"),
                "unique_field": "name",
                "schema": {
                    "name": {},
                    "qcode": {},
                },
            }
            self.post([cv])

    def get_article_cv_item(self, item, scheme):
        article_item = {k: v for k, v in item.items() if k not in ("is_active",)}
        article_item.update({"scheme": scheme})
        return article_item

    def get_items(
        self,
        _id: str,
        qcode: Optional[str] = None,
        is_active: bool = True,
        name: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> List:
        """
        Return `items` with specified filters from the CV with specified `_id`.
        If `lang` is provided then `name` is looked in `items.translations.name.{lang}`,
        otherwise `name` is looked in `items.name`.

        :param _id: custom vocabulary _id
        :param qcode: items.qcode filter
        :param is_active: items.is_active filter
        :param name: items.name filter
        :param lang: items.lang filter
        :return: items list
        """

        projection: Dict[str, Any] = {}
        lookup = {"_id": _id}

        if qcode:
            elem_match = projection.setdefault("items", {}).setdefault("$elemMatch", {})
            elem_match["qcode"] = qcode

        # if `lang` is provided `name` is looked in `translations.name.{lang}`
        if name and lang:
            elem_match = projection.setdefault("items", {}).setdefault("$elemMatch", {})
            elem_match[f"translations.name.{lang}"] = {
                "$regex": r"^{}$".format(name),
                # case-insensitive
                "$options": "i",
            }
        elif name:
            elem_match = projection.setdefault("items", {}).setdefault("$elemMatch", {})
            elem_match["name"] = {
                "$regex": r"^{}$".format(name),
                # case-insensitive
                "$options": "i",
            }

        cursor = self.get_from_mongo(req=None, lookup=lookup, projection=projection)

        try:
            items = cursor.next()["items"]
        except (StopIteration, KeyError):
            return []

        # $elemMatch projection contains only the first element matching the condition,
        # that"s why `is_active` filter is filtered via python
        if is_active is not None:
            items = [i for i in items if i.get("is_active", True) == is_active]

        def format_item(item):
            try:
                del item["is_active"]
            except KeyError:
                pass
            item["scheme"] = _id
            return item

        items = list(map(format_item, items))

        return items

    def get_languages(self):
        return self.get_items(_id="languages")

    def get_field_options(self, field) -> Dict:
        cv = self.find_one(req=None, _id=field)
        return cv and cv.get("field_options") or {}


def is_related_content(item_name, related_content=None):
    if related_content is None:
        related_content = list(
            get_resource_service("vocabularies").get(req=None, lookup={"field_type": "related_content"})
        )

    if related_content and item_name.split("--")[0] in [content["_id"] for content in related_content]:
        return True

    return False
