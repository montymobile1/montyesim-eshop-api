"""Admin API: users. Owned by MS-5077 (lookup), later MS-5079."""
from app.api.v1.admin._lookup import add_lookup_route
from app.api.v1.admin._route import admin_router

router = admin_router()

# MS-5077: GET /lookup. Keep it above any /{id} route.
add_lookup_route(router, "users", "admin.users.read")
