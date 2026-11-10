"""Authorization matrix: every state-changing route against the wrong actor."""
import pytest

from tests.helpers import query
from tests.review_helpers import VERSION_FORM
from tests.test_routes import Browser, alice, bob, create_task, choose, fire, reviewer, state  # noqa: F401


def snapshot(app):
    return [query(app, f"SELECT COUNT(*) FROM {t}")[0][0] for t in
            ("tasks", "evidence", "share_links", "review_assignments", "state_transitions", "projects")]


TASK_ROUTES = [("get", "/tasks/{t}"), ("get", "/tasks/{t}/edit"), ("post", "/tasks/{t}/edit"),
               ("post", "/tasks/{t}/delete"), ("post", "/tasks/{t}/complete"),
               ("post", "/tasks/{t}/events/START"), ("post", "/tasks/{t}/evidence"),
               ("post", "/tasks/{t}/evidence/1/delete"), ("post", "/tasks/{t}/reviewer"),
               ("post", "/tasks/{t}/share"), ("post", "/tasks/{t}/unshare"),
               ("get", "/tasks/{t}/reviewers"), ("post", "/tasks/{t}/reviewers/select")]
REVIEW_ROUTES = [("get", "/reviews/{t}"), ("post", "/reviews/{t}/version"), ("post", "/reviews/{t}/report"),
                 ("post", "/reviews/{t}/share"), ("post", "/reviews/{t}/unshare")]


def denied(response):
    """404/403, or the by-design redirect that sends a reviewer to their own queue."""
    if response.status_code == 302:
        return response.headers["Location"].rstrip("/").endswith("/review")
    return response.status_code in (403, 404)


def hit(browser, method, url):
    return browser.get(url) if method == "get" else browser.post(url, {"title": "x", "round": "1"})


@pytest.mark.parametrize("method,url", TASK_ROUTES)
def test_other_developer_and_unassigned_reviewer_get_404_on_task_routes(app, alice, bob, reviewer, method, url):
    task_id = create_task(alice, "Private task")
    before = snapshot(app)
    for outsider in (bob, reviewer):
        assert denied(hit(outsider, method, url.format(t=task_id)))
    assert snapshot(app) == before and state(app, task_id) == "CREATED"


@pytest.mark.parametrize("method,url", REVIEW_ROUTES)
def test_review_routes_hide_requests_from_outsiders(app, alice, bob, reviewer, method, url):
    r = alice.post("/projects/new", {"title": "P", "status": "Active"})
    pid = query(app, "SELECT id FROM projects")[0][0]
    r = alice.post("/reviews/new", dict(VERSION_FORM, project_id=str(pid), title="R"))
    task_id = int(r.headers["Location"].rsplit("/", 1)[1])
    before = snapshot(app)
    for outsider in (bob, reviewer):          # reviewer was never assigned
        assert denied(hit(outsider, method, url.format(t=task_id)))
    assert snapshot(app) == before


@pytest.mark.parametrize("method,url", [("get", "/dashboard"), ("get", "/add-task"), ("post", "/add-task"),
                                        ("get", "/projects"), ("get", "/projects/new"), ("post", "/projects/new"),
                                        ("get", "/review"), ("get", "/reviewers"), ("get", "/reviews/new"),
                                        ("get", "/reviewer/profile"), ("post", "/tasks/1/delete"),
                                        ("post", "/findings/1/update"), ("get", "/tasks/1"), ("get", "/reviews/1")])
def test_anonymous_requests_are_redirected_to_login(app, method, url):
    anon = Browser(app)
    response = anon.get(url) if method == "get" else anon.post(url, {}, csrf=False)
    assert response.status_code in (302, 400, 401, 403)
    assert response.status_code != 200
    if response.status_code == 302:
        assert "/login" in response.headers["Location"]


def test_task_cannot_be_attached_to_someone_elses_project(app, alice, bob):
    bob.post("/projects/new", {"title": "Bob's project", "status": "Active"})
    bob_project = query(app, "SELECT id FROM projects")[0][0]
    alice.post("/add-task", {"title": "sneaky", "project_id": str(bob_project)})
    rows = query(app, "SELECT project_id FROM tasks WHERE title = 'sneaky'")
    assert all(r[0] is None for r in rows)


def test_review_request_cannot_use_someone_elses_project(app, alice, bob):
    bob.post("/projects/new", {"title": "Bob's project", "status": "Active"})
    bob_project = query(app, "SELECT id FROM projects")[0][0]
    alice.post("/reviews/new", dict(VERSION_FORM, project_id=str(bob_project), title="steal"))
    assert query(app, "SELECT COUNT(*) FROM tasks WHERE title = 'steal'")[0][0] == 0


def test_projects_list_is_private_and_no_project_edit_or_delete_routes_exist(app, alice, bob):
    alice.post("/projects/new", {"title": "Alice secret project", "status": "Active"})
    pid = query(app, "SELECT id FROM projects")[0][0]
    assert "Alice secret project" not in bob.get("/projects").get_data(as_text=True)
    for method, url in (("get", f"/projects/{pid}"), ("get", f"/projects/{pid}/edit"),
                        ("post", f"/projects/{pid}/edit"), ("post", f"/projects/{pid}/delete")):
        assert hit(alice, method, url).status_code in (404, 405)    # documented: not implemented
    assert query(app, "SELECT COUNT(*) FROM projects")[0][0] == 1


def test_profile_pages_expose_only_published_data(app, alice, reviewer):
    page = Browser(app)
    assert page.get("/u/alice").status_code == 200
    html = page.get("/u/alice").get_data(as_text=True)
    assert "alice@example.com" not in html
    assert page.get("/u/rev").status_code == 404                   # reviewers have no developer profile
    assert page.get("/u/nobody").status_code == 404
    assert page.get("/reviewers/nobody").status_code == 404
    assert alice.get("/reviewer/profile").status_code in (403, 404)  # a developer has no reviewer profile page


def test_edit_page_renders_for_owner_and_saves_changes(app, alice):
    """Regression: GET /tasks/<id>/edit used to 500 (sqlite3.Row has no .get in the template)."""
    task_id = create_task(alice, "Original title")
    page = alice.get(f"/tasks/{task_id}/edit")
    assert page.status_code == 200 and "Original title" in page.get_data(as_text=True)
    assert alice.post(f"/tasks/{task_id}/edit", {"title": "New title", "priority": "high"}).status_code == 302
    assert query(app, "SELECT title, priority FROM tasks")[0][:] == ("New title", "high")
