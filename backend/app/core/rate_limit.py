from slowapi import Limiter
from slowapi.util import get_remote_address

# Single shared limiter instance — imported by main.py (to register the error
# handler) and by individual route modules (to apply @limiter.limit decorators).
# Uses in-memory storage, which is correct for a single-EC2 deployment.
limiter = Limiter(key_func=get_remote_address)
