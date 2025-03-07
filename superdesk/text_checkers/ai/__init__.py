# Copyright 2020 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

from typing import Any
from superdesk.resource import Resource
from superdesk.services import BaseService
from superdesk.errors import SuperdeskApiError
from superdesk.utils import AllowedContainer
from .. import tools
from .base import registered_ai_services, AIServiceBase
import superdesk

#: main endpoint to interact with AI Services
AI_SERVICE_ENDPOINT = "ai"

#: endpoint to manipulate AI Services data
AI_DATA_OP_ENDPOINT = "ai_data_op"

#: endpoint to image suggestion based on AI service tags
AI_IMAGE_SUGGESTIONS_ENDPOINT = "ai_image_suggestions"


class AIResource(Resource):
    schema = {
        "service": {
            "type": "string",
            "required": True,
        },
        "item": {
            "type": "dict",
            "required": True,
            "schema": {
                "guid": {"type": "string", "required": True},
                "abstract": {"type": "string", "required": False},
                "language": {"type": "string", "required": True},
                "headline": {"type": "string", "nullable": True},
                "body_html": {"type": "string", "required": True},
            },
        },
    }
    datasource = {"projection": {"analysis": 1}}
    internal_resource = False
    resource_methods = ["POST"]
    item_methods = []


class AIService(BaseService):
    r"""Service managing article analysis with machine learning/AI related services

    When doing a POST request on this service, the following keys can be used (keys
    with a\* are required)

    ==========  ===========
    key         explanation
    ==========  ===========
    service \*  name of the service to use
    item \*     item metadata to be analyzed
    ==========  ===========

    e.g. to get autotagging with iMatrics service:

    .. sourcecode:: json

        {
            "service": "imatrics",
            "item": {
                "guid": "some_id",
                "headline": "item headline",
                "body_html": "item content"
            }
        }

    """

    def create(self, docs, **kwargs):
        doc = docs[0]
        service = doc["service"]
        item = doc["item"]
        try:
            service = registered_ai_services[service]
        except KeyError:
            raise SuperdeskApiError.notFoundError("{service} service can't be found".format(service=service))

        analyzed_data = service.analyze(item)
        docs[0].update({"analysis": analyzed_data})
        return [0]

class AIDataOpResource(Resource):
    schema = {
        "service": {
            "type": "string",
            "required": True,
        },
        "operation": {
            "type": "string",
            "required": True,
            "allowed": ["search", "create", "delete", "feedback"],
        },
        "data_name": {
            "type": "string",
            "required": False,
        },
        "data": {
            "type": "dict",
            "required": True,
        },
    }
    datasource = {"projection": {"result": 1}}
    internal_resource = False
    resource_methods = ["POST"]
    item_methods = []


class AIDataOpService(BaseService):
    r"""Service to manipulate AI service related data

    When doing a POST request on this service, the following keys can be used (keys
    with a \* are required):


    ============  ===========
    key           explanation
    ============  ===========
    service \*    name of the service to use
    operation \*  operation that you want to do on the data (search, create, …)
    data_name     type of data to manipulate (empty for default or when service only
                  handle one type of data)
    data          argument of use for the operation
    ============  ===========

    e.g. to search for tags with iMatrics service:

    .. sourcecode:: json

        {
            "service": "imatrics",
            "operation": "search",
            "data": {"term": "some_term"},
        }
    """

    def create(self, docs, **kwargs):
        doc = docs[0]
        service = doc["service"]
        operation = doc["operation"]
        name = doc.get("data_name")
        data = doc["data"]
        try:
            service = registered_ai_services[service]
        except KeyError:
            raise SuperdeskApiError.notFoundError("{service} service can't be found".format(service=service))

        result = service.data_operation("POST", operation, name, data)
        docs[0].update({"result": result})
        return [0]


class AIImageResource(Resource):
    schema = {
        "service": {
            "type": "string",
            "required": True,
        },
        "items": {
            "type": "list",
            "required": True,
            "schema": {
                "type": "dict",
                "schema": {
                    "title": {"type": "string", "required": True},
                    "type": {"type": "string", "required": False},
                    "pubStatus": {"type": "boolean", "required": False},
                    "weight": {"type": "integer", "required": False},
                },
            },
        },
    }
    datasource = {"projection": {"result": 1}}
    internal_resource = False
    resource_methods = ["POST"]
    item_methods = []


class AIImageSuggestionService(BaseService):
    r"""Service to get image suggestions

    When doing a POST request on this service, the following keys can be used (keys
    with a \* are required):


    ============  ===========
    key           explanation
    ============  ===========
    service \*    name of the service to use
    items          argument of use for the operation
    ============  ===========

    e.g. to search for tags with iMatrics service:

    .. sourcecode:: json

        {
            "service": "imatrics_image_suggestions",
            "items": [{"title": "some_string", "type": "some_string", "pubStatus": "some_boolean", "weight": "some_integer"}],
        }
    """

    def create(self, docs, **kwargs):
        doc = docs[0]
        service = doc["service"]
        items = doc["items"]
        try:
            service = registered_ai_services[service]
        except KeyError:
            raise SuperdeskApiError.notFoundError("{service} service can't be found".format(service=service))

        res_data = service.search_images(items)
        docs[0].update({"result": res_data})
        return [0]


def init_app(app) -> None:
    allowed_service = AllowedContainer(registered_ai_services)

    endpoint_name = AI_SERVICE_ENDPOINT
    service: Any = AIService(endpoint_name, backend=superdesk.get_backend())
    AIResource.schema["service"]["allowed"] = allowed_service
    AIResource(endpoint_name, app=app, service=service)
    superdesk.intrinsic_privilege(endpoint_name, method=["POST"])

    endpoint_name = AI_DATA_OP_ENDPOINT
    service = AIDataOpService(endpoint_name, backend=superdesk.get_backend())
    AIDataOpResource.schema["service"]["allowed"] = allowed_service
    AIDataOpResource(endpoint_name, app=app, service=service)
    superdesk.intrinsic_privilege(endpoint_name, method=["POST"])

    endpoint_name = AI_IMAGE_SUGGESTIONS_ENDPOINT
    service = AIImageSuggestionService(endpoint_name, backend=superdesk.get_backend())
    AIImageResource.schema["service"]["allowed"] = allowed_service
    AIImageResource(endpoint_name, app=app, service=service)
    superdesk.intrinsic_privilege(endpoint_name, method=["POST"])