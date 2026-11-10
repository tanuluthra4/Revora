"""Logo integrity and honest-wording checks."""
import hashlib
import os
import re

from PIL import Image

from tests.test_review_routes import *  # noqa: F401,F403  (fixtures)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAND = os.path.join(ROOT, "static", "brand")
MASTER_SHA256 = "978871e06ede5857232f403f3d2802e47a42317f0f08bcbdfab77a0494970af9"


def test_logo_master_is_byte_identical_to_the_supplied_file():
    data = open(os.path.join(BRAND, "revisely-logo-original.png"), "rb").read()
    assert hashlib.sha256(data).hexdigest() == MASTER_SHA256
    assert len(data) == 1131966


def test_every_derivative_is_a_square_downscale_of_the_master():
    master = Image.open(os.path.join(BRAND, "revisely-logo-original.png")).convert("RGB")
    assert master.size == (1254, 1254)
    for name in os.listdir(BRAND):
        if name == "revisely-logo-original.png" or not name.endswith(".png"):
            continue
        im = Image.open(os.path.join(BRAND, name)).convert("RGB")
        assert im.size[0] == im.size[1] < 1254, name                      # proportional, smaller
        again = master.resize(im.size, Image.LANCZOS)
        diff = sum(abs(a - b) for p, q in zip(im.getdata(), again.getdata()) for a, b in zip(p, q))
        assert diff / (im.size[0] * im.size[1] * 3) < 2, name              # same picture, nothing added


def test_pages_reference_the_brand_icons(app):
    html = Browser(app).get("/").get_data(as_text=True)
    for f in ("brand/favicon-32.png", "brand/apple-touch-icon.png", "brand/revisely-logo-256.png"):
        assert f in html
        assert Browser(app).get("/static/" + f).status_code == 200
    assert Browser(app).get("/favicon.ico").status_code == 200


BANNED = ("verified expert", "expert-reviewed", "certified reviewer", "industry-certified", "guaranteed",
          "guarantees you", "job-ready", "approved by experts", "trusted by", "proves independence",
          "independent reviewers verified", "verified reviewer", "top-rated", "endorsed by")


def _texts():
    out = []
    for folder in ("templates", "docs"):
        for dp, _, files in os.walk(os.path.join(ROOT, folder)):
            out += [os.path.join(dp, f) for f in files if f.endswith((".html", ".md"))]
    return out + [os.path.join(ROOT, "README.md")]


def test_no_overclaiming_wording_anywhere():
    for path in _texts():
        text = open(path, encoding="utf-8").read().lower()
        for phrase in BANNED:
            assert phrase not in text, f"{phrase!r} in {path}"


def test_public_and_landing_pages_carry_the_honesty_statements(app, alice, rev):
    landing = Browser(app).get("/").get_data(as_text=True)
    assert "not a certification" in landing.lower()
    assert "do not prove that reviewers are independent" in landing
    assert "cannot tell whether one person controls several accounts" in landing
    pid = make_project(app, 1)
    alice.post("/reviews/new", dict(VERSION_FORM, project_id=str(pid), title="t"))
    assert "separate accounts do not prove that reviewers are independent" in \
        alice.get("/reviews/1").get_data(as_text=True).lower()


import subprocess


def test_no_secrets_or_databases_in_the_tree():
    gitignore = os.path.join(ROOT, ".gitignore")
    assert os.path.isfile(gitignore), ".gitignore is missing"

    rules = open(gitignore, encoding="utf-8").read().splitlines()
    assert "*.db" in rules
    assert "*.db-wal" in rules
    assert "*.db-shm" in rules
    assert "*.bak" in rules
    assert ".env" in rules

    allowed_runtime_db = os.path.normcase(
        os.path.join(ROOT, "database", "pow_os.db")
    )

    for dp, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".db", ".db-wal", ".db-shm", ".bak", ".env")):
                path = os.path.normcase(os.path.join(dp, f))
                assert path == allowed_runtime_db, path