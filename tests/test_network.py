import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from jobfinder import network
from test_jobfinder import ISO, FakeSession, Resp

CSV = '''Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Anna,Schmidt,https://www.linkedin.com/in/anna,,adesso SE,Senior Software Engineer,01 Jan 2024
Ben,Meyer,https://www.linkedin.com/in/ben,,adesso SE,Talent Acquisition Partner,02 Jan 2024
Cara,Lang,https://www.linkedin.com/in/cara,,adesso SE,Sales Manager,03 Jan 2024
Dev,Patel,https://www.linkedin.com/in/dev,,Storyblok,Data Engineer,04 Jan 2024
Eva,Klein,https://www.linkedin.com/in/eva,,Selbstständig,Consultant,05 Jan 2024
Finn,Roth,https://www.linkedin.com/in/finn,,Acme Robotics,ML Engineer,06 Jan 2024
'''


def setup(tmp_path):
    (tmp_path / "network").mkdir()
    (tmp_path / "network" / "connections.csv").write_text(CSV, encoding="utf-8")
    (tmp_path / "companies.yaml").write_text("- {name: Storyblok, ats: ashby, id: storyblok}\n")
    return tmp_path


def test_contacts_show_recruiters_first_and_match_legal_forms(tmp_path):
    net = network.load(setup(tmp_path))
    c = network.contacts(net, "adesso SE")
    assert c["total"] == 3 and c["company"] == "adesso SE"
    assert [p["name"] for p in c["people"]] == ["Ben Meyer", "Anna Schmidt", "Cara Lang"]
    assert network.contacts(net, "adesso")["exact"]                 # "adesso" vs "adesso SE": same after legal form
    near = network.contacts(net, "Acme Robotics Europe")
    assert near["company"] == "Acme Robotics" and not near["exact"]
    assert network.contacts(net, "Adidas") is None


def test_probe_adds_every_verified_careers_page(tmp_path):
    root = setup(tmp_path)
    s = FakeSession({
        "boards-api.greenhouse.io/v1/boards/adesso/jobs": Resp({"jobs": [
            {"id": 1, "title": "Python Developer", "absolute_url": "https://x/1", "location": {"name": "Dortmund"},
             "first_published": ISO(1), "content": "Python"}]}),
        "v1/boards/adesso": Resp({"name": "adesso SE"}),
        "jobs.ashbyhq.com/acmerobotics": Resp("<html><title>Acme Robotics Jobs</title></html>"),
        "api.ashbyhq.com/posting-api/job-board/acmerobotics": Resp({"jobs": [
            {"title": "ML Engineer", "jobUrl": "https://y/1", "location": "San Francisco, CA", "publishedAt": ISO(1),
             "isListed": True, "descriptionPlain": "x"}]}),
    })
    p = network.Prober(root, lambda: s, log=lambda m: None, workers=2)
    assert p.pending() == ["adesso SE", "Acme Robotics"]          # Storyblok known, Selbstständig skipped
    p.run()
    added = yaml.safe_load((root / network.OUT_FILE).read_text())
    # Acme has no job in Germany today but its board is real: read daily, so a future German job shows up
    assert [(c["name"], c["ats"], c["id"]) for c in added] == [("adesso SE", "greenhouse", "adesso"),
                                                               ("Acme Robotics", "ashby", "acmerobotics")]
    assert p.pending() == []                                        # resumable: nothing probed twice
    assert p.progress["found"] == 2


def test_board_must_belong_to_the_company():
    b = network.board_is_company
    assert b("KNIME", "knime", "KNIME GmbH")                       # Personio <subcompany>
    assert b("Westwing", "westwing", "")                           # no name given: exact full-name board
    assert not b("Thomas Henry GmbH", "thomas", "")                # shortened board, no name: no
    assert not b("Amazon", "amazon", "Amazon Pflege GmbH")
    assert b("elbkapitäne", "elbkapitaene", "Jobs bei elbkapit&auml;ne GmbH &amp; Co. KG")
    assert b("Trackman", "trackman", "Trackman Careers | Explore Jobs &amp; Join Our Global Team")
    assert not b("karriere.at GmbH", "karriere", "Karriere bei Herta")
    m = network.name_matches
    assert not m("Amazon", "Jobs at ")                      # empty Personio account named "amazon"
    assert not m("Amazon", "Jobs at Amazon Pflege GmbH")
    assert m("OMMAX - Digital Solutions", "Jobs at OMMAX GmbH")
    assert m("diva-e", "Jobs at diva-e Digital Value Excellence GmbH")
    assert m("DEPT®", "DEPT") and m("schoene neue kinder GmbH", "Jobs at schoene neue kinder GmbH")


def test_slugs():
    assert network.slugs("Schwarz Digits GmbH & Co. KG") == ["schwarzdigits", "schwarz-digits", "schwarz",
                                                             "schwarzdigitsgmbh"]
    assert "arxroboticsgmbh" in network.slugs("ARX Robotics GmbH")
    assert network.slugs("Digital Ventures") == ["digitalventures", "digital-ventures", "digitalventuresgmbh"]
    assert network.slugs("Glow25") == ["glow25"]


def test_empty_workable_accounts_do_not_count():
    s = FakeSession({"accounts/apple": Resp({"name": "Apple", "description": None, "jobs": []}),
                     "accounts/epay": Resp({"name": "epay", "jobs": [{"title": "Developer"}]})})
    assert not network._exists(s, "workable", "apple")
    assert network._exists(s, "workable", "epay")
    assert not network._exists(s, "workable", "madeupname")       # 404
