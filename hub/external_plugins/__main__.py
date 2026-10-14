"""Offline developer helpers: python -m hub.external_plugins MANIFEST.json."""
import argparse
import json
from pathlib import Path

from hub.external_plugins.contracts import parse_spec, public_definition
from hub.gateway.catalog import fingerprint


def main():
    parser = argparse.ArgumentParser(description='Validate a declarative CodePier plugin without credentials or network calls.')
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    try:
        if args.manifest.stat().st_size > 262144:
            raise ValueError('Manifest exceeds 256 KiB')
        spec = parse_spec(json.loads(args.manifest.read_text(encoding='utf-8')))
        result = {'valid': True, 'manifest_hash': fingerprint(spec.model_dump()),
                  'actions': [public_definition('preview', name, action) for name, action in spec.actions.items()]}
    except Exception as exc:
        parser.exit(1, f'Invalid manifest ({type(exc).__name__}); no plugin code was imported or executed.\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
