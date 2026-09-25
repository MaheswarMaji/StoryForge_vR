# StoryForge — Auth Testing Playbook (Google Sign-In)

## How auth works here
- Login is Google-only and optional (only the Admin Console needs it). The login page loads Google Identity Services
  using the `GOOGLE_CLIENT_ID` served by `GET /api/auth/config`.
- Google returns an ID token to the browser, which POSTs `{credential}` to `/api/auth/session`.
- The backend verifies the token directly with Google (`google-auth`), creates/updates the user, stores a 7-day session
  and sets the httpOnly cookie `session_token` (Lax on http; Secure + SameSite=None when `COOKIE_SECURE=true`).
- The first user to sign in becomes admin; emails in `ADMIN_EMAILS` are always admin.
- Cookie OR `Authorization: Bearer <session_token>` is accepted.

## Step 1: Create a test session directly in Mongo
mongosh --eval "
use('test_database');
var userId = 'test-user-' + Date.now();
var sessionToken = 'test_session_' + Date.now();
db.users.insertOne({
  user_id: userId,
  email: 'test.user.' + Date.now() + '@example.com',
  name: 'Test User',
  picture: 'https://via.placeholder.com/150',
  created_at: new Date()
});
db.user_sessions.insertOne({
  user_id: userId,
  session_token: sessionToken,
  expires_at: new Date(Date.now() + 7*24*60*60*1000),
  created_at: new Date()
});
print('SESSION_TOKEN=' + sessionToken);
"

## Step 2: Test backend API (use the external preview URL, not localhost)
# Admin routes without a session -> 401
curl -s -o /dev/null -w "%{http_code}" https://<preview-url>/api/admin/overview
# Authenticated -> 200
curl -s https://<preview-url>/api/auth/me -H "Authorization: Bearer SESSION_TOKEN"
curl -s https://<preview-url>/api/stories -H "Authorization: Bearer SESSION_TOKEN"
curl -s https://<preview-url>/api/settings/instagram -H "Authorization: Bearer SESSION_TOKEN"

## Step 3: Browser testing
await page.context.add_cookies([{
    "name": "session_token",
    "value": "SESSION_TOKEN",
    "domain": "<preview-host-without-scheme>",
    "path": "/",
    "httpOnly": true,
    "secure": true,
    "sameSite": "None"
}]);
await page.goto("https://<preview-url>/dashboard");  // must NOT bounce to /login

## Checklist
- `/api/auth/me` returns user data with user_id/email/name/picture
- Admin endpoints return 401/403 without an admin session
- With cookie/bearer: dashboard loads, stories list loads
- `/login` shows the Google-only login screen
- Logout clears session: POST /api/auth/logout then /auth/me -> 401

## Cleanup
mongosh --eval "
use('test_database');
db.users.deleteMany({email: /test\.user\./});
db.user_sessions.deleteMany({session_token: /test_session/});
"
