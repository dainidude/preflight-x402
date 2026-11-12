import os

print("=== QUICK STATUS CHECK ===")

# Check critical files
critical_files = [
    ('src/preflight/api/app.py', 'FastAPI app'),
    ('src/preflight/api/payments.py', 'x402 payment wiring'),
    ('src/preflight/api/cache.py', 'LRU cache'),
    ('src/preflight/api/ratelimit.py', 'rate limiting'),
    ('src/preflight/core/pipeline.py', 'mock core pipeline (should be deleted)'),
    ('tests/test_api_basic.py', 'basic tests'),
    ('tests/test_api_complete.py', 'comprehensive tests'),
    ('docs/STATUS_api.md', 'status documentation')
]

print("\nFile Status:")
for file, desc in critical_files:
    exists = os.path.exists(file)
    status = "✅" if exists else "❌"
    print(f"{status} {file:40} ({desc})")

# Check if pipeline.py exists (should not exist)
if os.path.exists('src/preflight/core/pipeline.py'):
    print("\n❌ ISSUE: src/preflight/core/pipeline.py exists (should be deleted)")
else:
    print("\n✅ src/preflight/core/pipeline.py does not exist (good)")

print("\n=== FIX ROUND REQUIRED ===")
print("1. Delete src/preflight/core/pipeline.py")
print("2. Create REAL x402 integration in payments.py")
print("3. Fix 4 failing tests (route keys + FREE_TIER)")
print("4. Update docs/STATUS_api.md")
print("5. Run tests to verify")
