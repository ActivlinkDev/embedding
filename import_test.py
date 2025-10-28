import importlib, traceback

try:
    importlib.import_module('routers.sku.create_custom_sku')
    print('IMPORT_OK')
except Exception:
    traceback.print_exc()
