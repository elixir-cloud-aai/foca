# FOCA-Petstore

Dockerized [Petstore][res-petstore] example application implemented using
[FOCA][res-foca].

## Description

The example demonstrates how FOCA sets up a fully configured [Flask][res-flask]
app when passed an appropriately formatted [configuration
file][docs-config-file].

FOCA makes sure that

* the returned app instance contains all [configuration parameters][app-config]
* FOCA configuration parameters are validated
* requests and responses sent to/from the API endpoints configured in the
  [Petstore][app-specs] [OpenAPI][res-openapi] specification are validated
* a [MongoDB][res-mongo-db] collection to store pets in is registered with The
  app
* [CORS][res-cors] is enabled
* handles exceptions (here only returns `500 / Internal Server Error` for all
  problems)

Apart from writing the configuration file, all that was left for us to do to
set up this example app was to write a _very_ simple app [entry point
module][app-entry-point], implement the [endpoint controller
logic][app-controllers] and prepare the [`Dockerfile`][app-dockerfile] and
[Docker Compose][res-docker-compose] [configuration][app-docker-compose] for
easy shipping/installation!

![Hint][img-hint] _**Check the [FOCA documentation][docs] for further
details.**_

## Installation

### Requirements

Ensure you have the following software installed:

* Docker (19.03.4, build 9013bf583a)
* docker-compose (1.25.5)
* Git (2.17.1)

> Indicated versions were used for developing/testing. Other versions may or
> may not work. Please let us know if you encounter any issues with versions
> _newer_ than the listed ones.

### Deploy app

Clone repository:

```bash
git clone https://github.com/elixir-cloud-aai/foca.git
```

Traverse to example app directory:

```bash
cd foca/examples/petstore
```

Build and run services in detached/daemonized mode:

```bash
docker-compose up -d --build
```

> In case Docker complains about port conflicts or if any of the used ports are
> blocked by a firewall, you will need to re-map the conflicting port(s) in the
> [Docker Compose config][app-docker-compose]. In particular, for each of the
> services that failed to start because of a port conflict, you will need to
> change the **first** of the two numbers listed below the corresponding
> `ports` keyword to some unused/open port. Note that if you change the mapped
> port for the `app` service you will need to manually append it to `localhost`
> when you access the API (or the Swagger UI) in subsequent steps, like, e.g.,
> so: `http://localhost:8080/`.

That's it, you can now visit the application's [Swagger UI][res-swagger-ui] in
your browser, e.g.,:

```bash
firefox http://localhost/ui  # or use your browser of choice
```

> Mac users may need to replace `localhost` with `0.0.0.0`.

## Explore app

In the [Swagger UI][res-swagger-ui], you may use the `GET`/`POST` endpoints by
providing the required/desired values based on the indicated descriptions, then
hit the `Try it out!` button!

Alternatively, you can access the API endpoints programmatically, e.g., via
[`curl`][res-curl]:

* To **register a new pet**:

  ```console
  curl -X POST \
      --header 'Accept: application/json' \
      --header 'Content-Type: application/json' \
      -d '{"name":"You","tag":"cat"}' \
      'http://localhost/pets'
  ```

* To **retrieve all registered pets**:

  ```console
  curl -X GET \
      --header 'Accept: application/json' \
      'http://localhost/pets' 
  ```

* To **retrieve information on a specific pet**:

  ```console
  curl -X GET \
      --header 'Accept: application/json' \
      'http://localhost/pets/0' 
  ```

* To **delete a pet**:  :-(

  ```console
  curl -X DELETE \
      --header 'Accept: application/json' \
      'http://localhost/pets/0' 
  ```

## Modify app

You can make use of this example to create your own app. Just modify any or all
of the following:

* [FOCA configuration file][app-config]
* [API specification][app-specs]
* [Endpoint controller module][app-controllers]
* [Main application module][app-entrypoint]
* [Dockerfile][app-dockerfile]
* [Docker Compose configuration][app-docker-compose]

### Modifying FOCA

In case you want to change FOCA itself and want the code changes to be
reflected in the Petstore app, you will need to manually rebuild the FOCA
container image like so:

```bash
docker build -t elixircloud/foca:latest .  # execute in the FOCA root directory
```

Then re-build and start the Petstore app as described before:

```bash
docker-compose up --build -d  # execute in _this_ directory
```

[app-config]: config.yaml
[app-controllers]: controllers.py
[app-dockerfile]: Dockerfile
[app-docker-compose]: docker-compose.yaml
[app-entrypoint]: app.py
[app-specs]: petstore.yaml
[docs]: <https://foca.readthedocs.io/en/latest/>
[docs-config-file]: ../../README.md#configuration-file
[img-hint]: ../../images/hint.svg
[res-cors]: <https://flask-cors.readthedocs.io/en/latest/>
[res-curl]: <https://curl.se/>
[res-docker-compose]: <https://docs.docker.com/compose/>
[res-flask]: <http://flask.pocoo.org/>
[res-foca]: <https://pypi.org/project/foca/>
[res-mongo-db]: <https://www.mongodb.com/>
[res-openapi]: <https://www.openapis.org/>
[res-petstore]: <https://petstore.swagger.io/>
[res-swagger-ui]: <https://swagger.io/tools/swagger-ui/>
