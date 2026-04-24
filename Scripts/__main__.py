"""Enable ``python -m Scripts ...`` as a synonym for ``Scripts.orchestration.cli``."""
from Scripts.orchestration.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
