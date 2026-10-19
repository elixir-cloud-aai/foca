# FOCA-Petstore-Access-Control

Dockerized [Petstore][res-petstore] example application implemented using
[FOCA][res-foca] with [access control][res-access-control] enabled.

## Description

The example demonstrates how FOCA sets up a fully configured [Flask][res-flask]
app when passed an appropriately formatted [configuration
file][docs-config-file].

FOCA makes sure that

* the returned app instance contains all [configuration parameters][app-config]
* FOCA configuration parameters are validated
* requests and responses sent to/from the API endpoints configured in the
  [Petstore][app-specs] [OpenAPI][res-openapi] specification are validated
* requests are authenticated with bearer tokens issued by an OpenID Connect
  identity provider
* access control policies are enforced on all API endpoints
* a [MongoDB][res-mongo-db] collection to store pets in is registered with the
  app
* a [MongoDB][res-mongo-db] collection to store access control policies in
  is registered with the app
* [CORS][res-cors] is enabled
* exceptions are handled according to the definitions in the `exceptions`
  dictionary in module [`exceptions`][app-exceptions]

Apart from writing the configuration file, all that was left for us to do to
set up this example app was to write a _very_ simple app [entry point
module][app-entrypoint], implement the [endpoint controller
logic][app-controllers] and prepare the [`Dockerfile`][app-dockerfile] and
[Docker Compose][res-docker-compose] [configuration][app-docker-compose] for
easy shipping/installation!

![Hint][img-hint] _**Check the [FOCA documentation][docs] for further
details.**_

## Access control

Access control is configured entirely in the [configuration file][app-config]
(`security.access_control`): `enabled: True` registers access control (the
policy collection and the permissions API), which requires trusted token
issuers (`security.auth.issuers`), and with `enforce: True`, FOCA checks every request
to an operation that requires authentication against the policies, using the
`sub` claim of the validated bearer token as the subject (e.g.,
`user:alice`), the request path as the object and the HTTP method as the
action. Requests that no policy allows are denied with `403`.

The [access control model][app-model] supports resource-scoped roles. The
[entry point module][app-entrypoint] adds the following policies when the app
starts:

| Rule | Meaning |
| --- | --- |
| `p, *, /pets, (GET)\|(POST)` | Any authenticated user may list and add pets. |
| `p, owner, /pets/:id, (GET)\|(DELETE)` | Owners may view and delete their pets. |

The [controllers][app-controllers] only manage ownership, with FOCA's helpers:
`addPet` makes the creator of a pet its owner (e.g., `g2, user:alice, owner,
/pets/0`), and `deletePet` removes all roles granted on a deleted pet.

Subjects with the `admin` role may do everything, including managing
policies via the [permissions API](#explore-app). To make a user an admin,
add a rule like `g, user:bob, admin` to the access control database
(collection `policy_rules` of database `access_control_db`), or map a group
claim of your identity provider to the `admin` role via `claim_groups` and
`group_mapping` (see the commented example in the [configuration
file][app-config]).

## Installation

### Requirements

Ensure you have the following software installed:

* Docker (with Docker Compose)
* Git

You also need access to an OpenID Connect identity provider that issues
bearer tokens (JSON Web Tokens) and supports the OpenID Connect discovery
protocol, as FOCA validates tokens against the issuer's user info endpoint
and public keys.

### Deploy app

Clone repository:

```bash
git clone https://github.com/elixir-cloud-aai/foca.git
```

Traverse to the Petstore app (_this_) directory:

```bash
cd foca/examples/petstore-access-control
```

Build and run services in detached/daemonized mode:

```bash
docker compose up -d --build
```

Some notes:

> * This will build the app based on the _current_ state of your FOCA clone.
> That is, any changes that you may have introduced to FOCA will be reflected
> in your build.
> * By default, the app will be built based on the Python version set in the
> [Docker Compose config][app-docker-compose]. If you would like to build the
> FOCA image based on a different version of Python, you can set the Python
> image to be used as FOCA's base image by defining the environment variable
> `PETSTORE_PY_IMAGE` before executing the build command. For example:
>
>    ```bash
>    export PETSTORE_PY_IMAGE=3.12-slim-trixie
>    ```
>
> * In case Docker complains about port conflicts or if any of the used ports
> are blocked by a firewall, you will need to re-map the conflicting port(s) in
> the [Docker Compose config][app-docker-compose]. In particular, for each of
> the services that failed to start because of a port conflict, you will need
> to change the **first** of the two numbers listed below the corresponding
> `ports` keyword to some unused/open port.

That's it, you can now visit the application's [Swagger UI][res-swagger-ui] in
your browser, e.g.,:

* For petstore application endpoints
```bash
firefox http://localhost/ui  # or use your browser of choice
```

* For petstore application admin endpoints
```bash
firefox http://localhost/admin/access-control/ui/  # or use your browser of choice
```

Some notes:

> * Mac users may need to replace `localhost` with `0.0.0.0`.
> * If you have changed the mapped port for the `app` service you will need to
> manually append it to `localhost` when you access the API (or the Swagger UI)
> in subsequent steps. For example, assuming you would like to run the app on
> port `8080` instead of the default of `80`, then the app will be availabe
> at `http://localhost:8080/`.

## Explore app

All endpoints require a bearer token. Get one from your identity provider and
store it in an environment variable, e.g.:

```bash
export TOKEN=<your token>
```

In the [Swagger UI][res-swagger-ui], authorize with the token, then use the
endpoints by providing the required/desired values based on the indicated
descriptions, and hit the `Try it out!` button!

Alternatively, you can access the API endpoints programmatically, e.g., via
[`curl`][res-curl]:

* To **register a new pet** (you become its owner):

  ```console
  curl -X POST \
      --header "Authorization: Bearer ${TOKEN}" \
      --header 'Accept: application/json' \
      --header 'Content-Type: application/json' \
      -d '{"name":"You","tag":"cat"}' \
      'http://localhost/pets'
  ```

* To **retrieve all registered pets**:

  ```console
  curl -X GET \
      --header "Authorization: Bearer ${TOKEN}" \
      --header 'Accept: application/json' \
      'http://localhost/pets'
  ```

* To **retrieve information on a specific pet** (owners and admins only):

  ```console
  curl -X GET \
      --header "Authorization: Bearer ${TOKEN}" \
      --header 'Accept: application/json' \
      'http://localhost/pets/0'  # replace 0 with the the pet ID of choice
  ```

* To **delete a pet** (owners and admins only):  :-(

  ```console
  curl -X DELETE \
      --header "Authorization: Bearer ${TOKEN}" \
      --header 'Accept: application/json' \
      'http://localhost/pets/0'  # replace 0 with the the pet ID of choice
  ```

The permissions API can only be used by admins:

* To **register a new permission**, e.g., to make user `bob` an admin:

  ```console
  curl -X POST \
      --header "Authorization: Bearer ${TOKEN}" \
      --header 'Accept: application/json' \
      --header 'Content-Type: application/json' \
      -d '{"policy_type":"g","rule":{"v0":"user:bob","v1":"admin"}}' \
      'http://localhost/admin/access-control/permissions'
  ```

* To **update an existing permission**:

  ```console
  curl -X PUT \
      --header "Authorization: Bearer ${TOKEN}" \
      --header 'Accept: application/json' \
      --header 'Content-Type: application/json' \
      -d '{"policy_type":"p","rule":{"v0":"*","v1":"/pets","v2":"GET"}}' \
      'http://localhost/admin/access-control/permissions/abc123' # replace abc123 with the permission ID of choice
  ```

* To **retrieve registered permissions** page by page (`results`, with
  pagination metadata in `pagination`):

  ```console
  curl -X GET \
      --header "Authorization: Bearer ${TOKEN}" \
      --header 'Accept: application/json' \
      'http://localhost/admin/access-control/permissions?page_size=100&page=0'
  ```

* To **retrieve information on a specific permission**:

  ```console
  curl -X GET \
      --header "Authorization: Bearer ${TOKEN}" \
      --header 'Accept: application/json' \
      'http://localhost/admin/access-control/permissions/abc123'  # replace abc123 with the permission ID of choice
  ```

* To **delete a permission**:

  ```console
  curl -X DELETE \
      --header "Authorization: Bearer ${TOKEN}" \
      --header 'Accept: application/json' \
      'http://localhost/admin/access-control/permissions/abc123'  # replace abc123 with the permission ID of choice
  ```

## Modify app

You can make use of this example to create your own app. Just modify any or all
of the following:

* [FOCA configuration file][app-config]
* [Main application module][app-entrypoint]
* [API specification][app-specs]
* [Endpoint controller module][app-controllers]
* [Access control model][app-model]
* [Registered exceptions][app-exceptions]
* [`Dockerfile`][app-dockerfile]
* [`docker-compose` configuration][app-docker-compose]


[app-config]: config.yaml
[app-controllers]: controllers.py
[app-dockerfile]: Dockerfile
[app-docker-compose]: docker-compose.yaml
[app-exceptions]: exceptions.py
[app-entrypoint]: app.py
[app-model]: petstore_policies.conf
[app-specs]: petstore-access-control.yaml
[docs]: <https://foca.readthedocs.io/en/latest/>
[docs-config-file]: ../../README.md#configuration-file
[img-hint]: ../../images/hint.svg
[res-access-control]: <https://foca.readthedocs.io/en/latest/access-control/>
[res-cors]: <https://flask-cors.readthedocs.io/en/latest/>
[res-curl]: <https://curl.se/>
[res-docker-compose]: <https://docs.docker.com/compose/>
[res-flask]: <http://flask.pocoo.org/>
[res-foca]: <https://pypi.org/project/foca/>
[res-mongo-db]: <https://www.mongodb.com/>
[res-openapi]: <https://www.openapis.org/>
[res-petstore]: <https://petstore.swagger.io/>
[res-swagger-ui]: <https://swagger.io/tools/swagger-ui/>
