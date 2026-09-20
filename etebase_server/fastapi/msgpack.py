import typing as t

from fastapi.routing import APIRoute, get_request_handler
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

# Resolve the effective route context (prefix + path params) the same way
# FastAPI's own get_route_handler does; without it, params that live in an
# include_router prefix would be seen as query params.
try:
    from fastapi.routing import _APIRouteLike, _effective_route_context_var
except ImportError:  # pragma: no cover

    def _resolve_effective_route(route):
        return route

else:

    def _resolve_effective_route(route):
        effective_context = _effective_route_context_var.get()
        if effective_context is not None and effective_context.original_route is route:
            return t.cast(_APIRouteLike, effective_context)
        return route


from .db_hack import django_db_cleanup_decorator
from .utils import msgpack_decode, msgpack_encode


class MsgpackRequest(Request):
    media_type = "application/msgpack"

    async def raw_body(self) -> bytes:
        return await super().body()

    async def body(self) -> bytes:
        if not hasattr(self, "_json"):
            body = await self.raw_body()
            self._json = msgpack_decode(body)
        return self._json


class MsgpackResponse(Response):
    media_type = "application/msgpack"

    def render(self, content: t.Optional[t.Any]) -> bytes:
        if content is None:
            return b""

        if isinstance(content, BaseModel):
            content = content.model_dump()
        return msgpack_encode(content)


class MsgpackRoute(APIRoute):
    # keep track of content-type -> request classes
    REQUESTS_CLASSES = {MsgpackRequest.media_type: MsgpackRequest}
    # keep track of content-type -> response classes
    ROUTES_HANDLERS_CLASSES = {MsgpackResponse.media_type: MsgpackResponse}

    def __init__(self, path: str, endpoint: t.Callable[..., t.Any], *args, **kwargs):
        endpoint = django_db_cleanup_decorator(endpoint)
        super().__init__(path, endpoint, *args, **kwargs)

    def _get_media_type_route_handler(self, media_type, route=None):
        route = route or self
        return get_request_handler(
            dependant=route.dependant,
            body_field=route.body_field,
            status_code=route.status_code,
            # use custom response class or fallback on default self.response_class
            response_class=self.ROUTES_HANDLERS_CLASSES.get(media_type, self.response_class),
            response_field=route.response_field,
            response_model_include=route.response_model_include,
            response_model_exclude=route.response_model_exclude,
            response_model_by_alias=route.response_model_by_alias,
            response_model_exclude_unset=route.response_model_exclude_unset,
            response_model_exclude_defaults=route.response_model_exclude_defaults,
            response_model_exclude_none=route.response_model_exclude_none,
            dependency_overrides_provider=route.dependency_overrides_provider,
            embed_body_fields=route._embed_body_fields,
            strict_content_type=route.strict_content_type,
            stream_item_field=route.stream_item_field,
            is_json_stream=route.is_json_stream,
        )

    def get_route_handler(self) -> t.Callable:
        route = _resolve_effective_route(self)

        async def custom_route_handler(request: Request) -> Response:
            content_type = request.headers.get("Content-Type")
            if content_type is not None:
                try:
                    request_cls = self.REQUESTS_CLASSES[content_type]
                    request = request_cls(request.scope, request.receive)
                except KeyError:
                    # nothing registered to handle content_type, process given requests as-is
                    pass

            accept = request.headers.get("Accept")
            route_handler = self._get_media_type_route_handler(accept, route)
            return await route_handler(request)

        return custom_route_handler
