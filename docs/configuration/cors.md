# `security.cors`

[Cross-origin resource sharing](https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS)
(CORS) lets web apps served from other origins call the API from browsers.

```yaml
security:
  cors:
    enabled: true
    allow_origins:
      - https://portal.example.org
    allow_methods: [GET, POST, DELETE]
    allow_headers: [Authorization, Content-Type]
    expose_headers: [X-Total-Count]
    allow_credentials: true
    max_age: 600
```

By default, CORS is enabled for requests from any origin, without
credentials (e.g., cookies). Bearer tokens in the `Authorization` header are
not affected by `allow_credentials`. Credentials can only be allowed for
explicitly listed origins, as allowing them for any origin would let any
website make authenticated requests on behalf of users.

::: foca.models.config.CORSConfig
    options:
      members: false
