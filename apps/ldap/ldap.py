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
import re
from ldap3 import Server, Connection, SUBTREE
from ldap3.core.exceptions import LDAPException
from apps.auth.service import AuthService
from superdesk.users.services import UsersService
from superdesk import get_resource_service
from superdesk.errors import SuperdeskApiError
from superdesk.resource import Resource
from flask import current_app as app
import superdesk
from apps.auth.errors import CredentialsAuthError
from apps.auth import get_user
from flask_babel import _

logger = logging.getLogger(__name__)


class ImportUserProfileResource(Resource):
    """
    Resource class used while adding a new user from UI when AD is active.
    """

    url = "import_profile"

    schema = {
        "username": {"type": "string", "required": True, "minlength": 1},
        "password": {"type": "string", "required": True, "minlength": 5},
        "profile_to_import": {"type": "string", "required": True, "minlength": 1},
    }

    datasource = {"source": "users", "projection": {"password": 0, "preferences": 0}}

    extra_response_fields = ["display_name", "username", "is_active", "needs_activation"]

    item_methods = []
    resource_methods = ["POST"]
    privileges = {"POST": "users"}


class ADAuth:
    """
    Handles Authentication against Active Directory.
    """

    def __init__(self, host, port, base_filter, user_filter, profile_attributes, fqdn):
        """Initializes the AD Server

        :param host: ldap server. for example ldap://aap.com.au
        :param port: default port is 389
        :param base_filter:
        :param user_filter:
        :param profile_attributes:
        """
        self.ldap_server = Server(host, (port if port is not None else 389))

        self.fqdn = fqdn
        self.base_filter = base_filter
        self.user_filter = user_filter
        self.profile_attrs = profile_attributes

    def authenticate_and_fetch_profile(self, username, password, username_for_profile=None):
        """Authenticates a user with credentials username and password against AD.

        If authentication is successful then it
        fetches a profile of a user identified by username_for_profile and if found the profile is returned.

        :param username: LDAP username
        :param password: LDAP password
        :param username_for_profile: Username of the profile to be fetched
        :return: user profile base on the LDAP_USER_ATTRIBUTES
        """

        if username_for_profile is None:
            username_for_profile = username

        if self.fqdn is not None and self.fqdn and "@" not in username:
            username = username + "@" + self.fqdn

        try:
            ldap_conn = Connection(self.ldap_server, auto_bind=True, user=username, password=password)

            user_filter = self.user_filter.format(username, username_for_profile.split("@")[0])
            logger.info("base filter:{} user filter:{}".format(self.base_filter, user_filter))

            with ldap_conn:
                result = ldap_conn.search(
                    self.base_filter, user_filter, search_scope=SUBTREE, attributes=list(self.profile_attrs.keys())
                )

                if not result:
                    # the search returns false in case of user not a security group member.
                    raise CredentialsAuthError(
                        credentials={"username": username},
                        message=_("User does not belong to security Group or could not find the user profile."),
                    )

                response = dict()
                user_profile = ldap_conn.response[0]["attributes"]

                for ad_profile_attr, sd_profile_attr in self.profile_attrs.items():
                    response[sd_profile_attr] = (
                        user_profile[ad_profile_attr] if user_profile.__contains__(ad_profile_attr) else ""
                    )

                    response[sd_profile_attr] = (
                        response[sd_profile_attr][0]
                        if isinstance(response[sd_profile_attr], list)
                        else response[sd_profile_attr]
                    )

                return response
        except LDAPException as e:
            raise CredentialsAuthError(credentials={"username": username}, error=e)


class ADAuthService(AuthService):
    def on_create(self, docs):

        user_service = get_resource_service("users")
        for doc in docs:
            user = self.authenticate(doc)

            if not user.get("_id"):
                user_service.post([user])

            self.set_auth_default(doc, user["_id"])

    def authenticate(self, credentials):
        """Authenticates the user against Active Directory

        :param credentials: an object having "username" and "password" attributes
        :return: if success returns User object, otherwise throws Error
        """
        settings = app.settings
        ad_auth = ADAuth(
            settings["LDAP_SERVER"],
            settings["LDAP_SERVER_PORT"],
            settings["LDAP_BASE_FILTER"],
            settings["LDAP_USER_FILTER"],
            settings["LDAP_USER_ATTRIBUTES"],
            settings["LDAP_FQDN"],
        )

        username = credentials.get("username")
        password = credentials.get("password")
        profile_to_import = credentials.get("profile_to_import", username)

        user_data = ad_auth.authenticate_and_fetch_profile(username, password, username_for_profile=profile_to_import)

        if len(user_data) == 0:
            raise SuperdeskApiError.notFoundError(
                message=_("No user has been found in AD"), payload={"profile_to_import": 1}
            )

        # If the LDAP server returned a username use that.
        if "username" in user_data:
            profile_to_import = user_data.pop("username", None)

        query = get_user_query(profile_to_import)

        user = superdesk.get_resource_service("users").find_one(req=None, **query)

        if (
            app.settings.get("LDAP_SET_DISPLAY_NAME", False)
            and "display_name" in user_data
            and all(f in user_data for f in app.settings.get("LDAP_SET_DISPLAY_NAME_FIELDS", []))
        ):
            user_data["display_name"] = app.settings.get("LDAP_SET_DISPLAY_NAME_FORMAT", "").format(
                *[user_data.get(f) for f in app.settings.get("LDAP_SET_DISPLAY_NAME_FIELDS", [])]
            )

        if not user:
            add_default_values(
                user_data, profile_to_import, user_type=None if "user_type" not in user_data else user_data["user_type"]
            )
            user = user_data
        else:
            superdesk.get_resource_service("users").patch(user.get("_id"), user_data)
            user = superdesk.get_resource_service("users").find_one(req=None, **query)

        return user


class ImportUserProfileService(UsersService):
    """Service Class for endpoint /import_profile"""

    def on_create(self, docs):

        logged_in_user = get_user().get("username")
        for index, doc in enumerate(docs):
            # ensuring the that logged in user is importing the profile.
            if logged_in_user != doc.get("username"):
                raise SuperdeskApiError.forbiddenError(message=_("Invalid Credentials."), payload={"credentials": 1})

            try:
                # authenticate on error sends 401 and the client is redirected to login.
                # but in case import user profile from Active Directory 403 should be fine.
                user = get_resource_service("auth_db").authenticate(doc)
            except CredentialsAuthError:
                raise SuperdeskApiError.forbiddenError(message=_("Invalid Credentials."), payload={"credentials": 1})

            if user.get("_id"):
                raise SuperdeskApiError.badRequestError(
                    message=_("User already exists in the system."), payload={"profile_to_import": 1}
                )

            docs[index] = user

        super().on_create(docs)


def add_default_values(doc, user_name, user_type, **kwargs):
    """
    Adds user_name, user_type, is_active: True, needs_activation: False and the values passed to **kwargs to doc.
    """

    doc["username"] = user_name.strip()
    doc["user_type"] = "user" if user_type is None else user_type
    doc["is_active"] = True
    doc["is_enabled"] = True
    doc["needs_activation"] = False
    doc.update(**kwargs)


def get_user_query(username):
    """Get the user query.

    :param str username:
    :return dict: query
    """
    pattern = "^{}$".format(re.escape(username.strip()))
    return {"username": re.compile(pattern, re.IGNORECASE)}
