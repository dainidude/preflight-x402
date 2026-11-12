import sys
print("Python path:", sys.path[:3])

try:
    from preflight.types import ScanRequest
    print("✓ ScanRequest import successful")
except Exception as e:
    print(f"✗ ScanRequest import failed: {e}")

try:
    from preflight.api.app import app
    print("✓ app import successful")
except Exception as e:
    print(f"✗ app import failed: {e}")
