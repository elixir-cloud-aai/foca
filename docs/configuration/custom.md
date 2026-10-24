# `custom`

App-specific settings can be kept in the same configuration file, in section
`custom`, and validated against a [pydantic](https://docs.pydantic.dev/)
model:

```yaml title="config.yaml"
custom:
  environment: production
  storage:
    url: https://storage.example.org
    max_size: 1000000
```

```python title="my_app/config.py"
from pydantic import BaseModel


class StorageConfig(BaseModel):
    url: str
    max_size: int = 100_000


class CustomConfig(BaseModel):
    environment: str = "development"
    storage: StorageConfig
```

```python title="my_app/app.py"
from foca import Foca

app = Foca(
    config_file="config.yaml",
    custom_config_model="my_app.config.CustomConfig",
).create_app()
```

The section is validated when the app is created; FOCA raises a `ValueError`
if it does not match the model. Afterwards, `get_config().custom` is an
instance of the model:

```python
from foca import get_config

max_size = get_config().custom.storage.max_size
```

FOCA keeps the section as an untyped extra field of its configuration, so
type checkers do not know the model. Use `get_custom_config()` to get it
typed, e.g., in a helper of your app:

```python title="my_app/config.py"
from foca import get_custom_config


def get_storage_config() -> StorageConfig:
    return get_custom_config(CustomConfig).storage
```

`get_custom_config()` raises a `RuntimeError` if the custom configuration is
missing or not an instance of the model, e.g., as `custom_config_model` is
not set. Set it to the full module path of the model (here,
`my_app.config.CustomConfig`) that your code imports: a model imported under
another module path is another class.

Without `custom_config_model`, the section (like any other unknown top-level
section) is passed through as is, as a dictionary.

Do not put secrets into configuration files; read them from environment
variables instead (e.g., with
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)).
