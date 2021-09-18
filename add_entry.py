import csv, urllib3, os, requests, time, zipfile, struct, binascii
from urllib.parse import urlparse
from typing import IO, Any, Dict, List
from io import BytesIO

import inquirer, dictdiffer

RESET = "\u001b[0m"
RED = "\u001b[31m"
GREEN = "\u001b[32m"
YELLOW = "\u001b[33m"


# session for **certain** website
s = requests.Session()
s.headers.update({
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4644.2 Safari/537.36"
})

gh_s = requests.Session()
gh_token = os.getenv("GITHUB_TOKEN") or "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"
gh_s.headers.update({
    "authorization": "Bearer "+gh_token
})

def crc32(string: str):
    return binascii.hexlify(binascii.crc32(string.encode("utf8")).to_bytes(4, "little")).decode("utf8")

def validate_not_empty(_, val):
    return val.strip() != ""

def validate_url(_, url):
    try:
        res = urlparse(url)
        return all([res.scheme, res.netloc, res.path])
    except: return False


# load the db
def load_db():
    f = open("cbpsdb.csv", "r", encoding="utf8")
    field_names = f.readline()[:-1].split(",")
    db = csv.DictReader(f.read().splitlines(False), field_names)
    f.close()
    return db

# remove the existing entry and add the updated one
def update_entry(db: List[Dict[str,Any]], fieldnames, entry):
    existing = list(filter(lambda k: k["title"] != entry["title"], db))
    add_entry(existing, fieldnames, entry)

# add to db
def add_entry(entries: List[Dict[str,Any]], fieldnames: List, entry, top=True):
    f = open("cbpsdb.csv", "w", encoding="utf8", newline="")
    db_writer = csv.DictWriter(f, fieldnames)
    db_writer.writeheader()
    if top:
        db_writer.writerow(entry)
    db_writer.writerows(entries)
    if not top:
        db_writer.writerow(entry)


# prompt to choose release
def prompt_gh_release(releases: Dict):
    if not len(releases):
        print("repository has no releases")
        return {}
    release_name = inquirer.list_input("release", choices=releases.keys())
    release = releases[release_name]
    assets = {item['name']: item for item in release['assets']}
    asset_name = inquirer.list_input("asset", choices=assets.keys())
    asset = assets[asset_name]
    return {"download_url": asset['browser_download_url']}

# get owner and repo from github url
def gh_get_repo_name(src_url: str):
    start = src_url.index("github.com") + 11
    owner, repo = src_url[start:].split("/")[:2]
    if repo.endswith(".git"): repo = repo[:-4]
    return owner, repo

# use authed github rest api
def gh_get(url: str):
    r = gh_s.get(url)
    if r.status_code != 200:
        raise Exception(f"(github) response: {r.status_code} {url}")
    return r.json()

# get download_url for an github repo
def get_download_github(src_url: str):
    owner, repo = gh_get_repo_name(src_url)
    data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/releases")

    releases = {item['tag_name']: item for item in data}
    return prompt_gh_release(releases)

# get download_readme for an github url
def get_readme_github(src_url: str):
    owner, repo = gh_get_repo_name(src_url)
    data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/contents/")

    files = {item['name'].lower(): item for item in data if item["type"] == "file"}
    files[None] = None
    default = ("readme.md" if files.get("readme.md") else None)
    download_readme = inquirer.list_input("download_readme", choices=files, default=default)
    if download_readme == None: return {}
    return {"download_readme": files[download_readme]["download_url"]}

# get download_icon0 for an github url
def get_icon0_github(src_url: str):
    owner, repo = gh_get_repo_name(src_url)
    branch = gh_get(f"https://api.github.com/repos/{owner}/{repo}")['default_branch']
    data = gh_get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1")

    all_files = {item["path"]: item for item in data["tree"]} # map to dict
    pngs = [None] + list(filter(lambda k: k.endswith(".png"), all_files.keys()))
    default = list(filter(lambda k: k is None or k.endswith("icon0.png"), pngs))[-1]

    download_icon0 = inquirer.list_input("download_icon0", choices=pngs, default=default)
    if download_icon0 != None:
        download_icon0 = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{download_icon0}"
    return {"download_icon0": download_icon0}

# get credits for an github url
def github_author(src_url: str):
    author, _ = gh_get_repo_name(src_url)
    credits = inquirer.text("credits", default=author, validate=validate_not_empty)
    return {"credits": credits}


# sfo parser
class IndexTableEntry:
    name: str
    value: str
    def __init__(self, f: IO[bytes], name_table_start: int, data_table_start) -> None:
        (
            name_offset,
            value_fmt,
            _, # value_len
            value_max_len,
            data_offset
        ) = struct.unpack("<HHIII", f.read(16))
        pos = f.tell()

        # read name
        f.seek(name_table_start + name_offset)
        self.name = f.read(1)
        while chr(self.name[-1]) != "\0":
            self.name += f.read(1)
        self.name = self.name[:-1].decode("ascii")

        # read value
        f.seek(data_table_start + data_offset)
        value = f.read(value_max_len)
        if value_fmt in [0x0204, 0x0004]:
            self.value = value.rstrip(b"\0").decode("utf8")
        elif value_fmt == 0x0404:
            self.value = struct.unpack("<I", value)[0]
        f.seek(pos)

    def __repr__(self) -> str:
        return str(self.value)

def simple_parse_sfo(f: IO[bytes]) -> Dict[str, Any]:
    (
        magic,
        _, # version
        name_table_offset,
        data_table_offset,
        index_table_entries
    ) = struct.unpack("<4sIIII", f.read(20))
    assert magic == b'\0PSF', "invalid sfo"

    index_table_bytes = 16 * index_table_entries
    index_table_padding = name_table_offset - 20 - index_table_bytes
    assert index_table_padding >= 0

    table = {}
    for _ in range(index_table_entries):
        entry = IndexTableEntry(f, name_table_offset, data_table_offset)
        table[entry.name] = entry.value
        f.read(index_table_padding)
    return table

# download vpk and read sfo in it to get titleid and title
def get_vpk(url: str):
    f = BytesIO()
    print(f"downloading: {url}")
    headers = s.headers
    if url.count("vitadb.rinnegatamante.it"):
        headers["referer"] = "https://vitadb.rinnegatamante.it/"

    r = s.get(url, stream=True, allow_redirects=True, headers=headers)
    r.raise_for_status()
    total_length = int(r.headers.get('content-length', 0))

    if total_length == 0:
        f.write(r.content)
    else:
        dl = 0
        for data in r.iter_content(1024*1024):
            dl += len(data)
            f.write(data)
            done = int(50 * dl / total_length)
            print(f"\r[{'=' * done}{' ' * (50-done)}] {2*done}%\033[K",end="")
        print("\n")
    f.seek(0)

    zip_file = zipfile.ZipFile(f)
    param_sfo = zip_file.open("sce_sys/param.sfo", "r")
    sfo = simple_parse_sfo(param_sfo)
    return {
        "id": sfo["TITLE_ID"],
        "title": sfo["TITLE"]
    }


def main():
    db = load_db()
    db_fieldnames = db.fieldnames
    db = list(db)
    new_entry = {k: None for k in db_fieldnames}

    # ask for repo
    new_entry["download_src"] = inquirer.text("source url, leave empty if none", validate=lambda _, k: validate_url(_,k) or k == "").strip()

    download_src = new_entry["download_src"]
    host = urllib3.get_host(download_src or "")
    if len(host) > 1 and host[1] == "github.com": # todo: add other hosts
        # download
        download = get_download_github(download_src)
        new_entry.update(download)
        del download
        # readme
        readme = get_readme_github(download_src)
        new_entry.update(readme)
        del readme
        #icon0
        icon0 = get_icon0_github(download_src)
        new_entry.update(icon0)
        del icon0
        #credits
        credits = github_author(download_src)
        new_entry.update(credits)
        del credits

    if not new_entry["download_url"]:
        new_entry["download_url"] = inquirer.text("vpk/prx download url",validate=validate_url)
        if not new_entry["download_url"]: exit(1)
    
    # get entry type from filename
    ext = new_entry["download_url"].split(".")[-1]
    entry_types = {"vpk": "VPK", "suprx": "PLUGIN", "skprx": "PLUGIN"}
    new_entry["type"] = inquirer.list_input("entry type", choices=["VPK", "PLUGIN", "DATA"], default=entry_types.get(ext, "DATA"))
    del ext, entry_types

    # what fields *must* be set
    required_fields = [
        "id", "title", "credits",
        "download_url",
        "visible", "type", "time_added"
    ]

    if new_entry["type"] == "VPK":
        required_fields.extend(["download_icon0"])
        vpk = get_vpk(new_entry["download_url"])
        new_entry.update(vpk)
        print(vpk)
        del vpk

    elif new_entry["type"] == "PLUGIN":
        required_fields.extend(["config_type", "options"])
        new_entry["config_type"] = inquirer.list_input("config_type", choices=["TAI", "BOOT"])

        if new_entry["config_type"] == "TAI":
            desc = "specify a list containing what modules to load under (eg: '*PCSI00009|*PCSI00007')"
        elif new_entry["config_type"] == "BOOT":
            raise Exception("not supported, do it manually AND READ THE (totally real) RFC")

    elif new_entry["type"] == "DATA":
        desc = "specify a string containing the path to extract the data files too."
    
    if new_entry["type"] in ("PLUGIN", "DATA"):
        new_entry["options"] = inquirer.text(desc, validate=validate_not_empty)

    print("Visible:")
    new_entry["visible"] = inquirer.list_input("visible", choices=[True,False], default=True)
    new_entry["time_added"] = int(time.time())

    print("entry:", new_entry)

    print("enter missing fields, leave empty if None")
    for field, value in new_entry.items():
        default = None
        if value == None:
            # set title before id
            if field == "id":
                if not new_entry["title"]:
                    answer = inquirer.text("title", validate=validate_not_empty)
                    new_entry["title"] = answer
                default = crc32(new_entry["title"])

            if field in required_fields:
                answer = inquirer.text("(REQUIRED) " + field, validate=validate_not_empty, default=default)
            else:
                answer = inquirer.text(field, default=default)
            new_entry[field] = answer

    # correct format for empty fields
    for field, value in new_entry.items():
        if value == None or value == "":
            new_entry[field] = "None"
        else:
            new_entry[field] = str(value).strip()
    
    i = 0
    while True:
        i+=1
        # if theres any that have the same id but not same title add _{i}   (ugly)
        same_id = [item for item in db if new_entry["id"] == item["id"]]
        same_id_different_title = [item for item in same_id if item["title"] != new_entry["title"]]
        if not len(same_id_different_title):
            break

        if new_entry["id"][-2] == "_": new_entry["id"] = new_entry["id"][-2]
        new_entry["id"] += f"_{i}"
    del i

    # add to db
    existing = [item for item in db if new_entry["title"] == item["title"]]
    if len(existing): # ask user if they want to update
        existing = existing[0]
        diff = dictdiffer.diff(existing, new_entry)
        for item in diff:
            print(f"{YELLOW}{item[0]}{RESET}: {item[1]}\t [{RED}{item[2][0]}{RESET}  {GREEN}{item[2][1]}{RESET}]")
        resp = inquirer.list_input("found existing entry, do you want to update it?", choices=["yes", "no"], default="no")
        resp = resp == "yes"
        if not resp:
            print("not updating")
            exit(0)
        else:
            print("updating in db")
            update_entry(db, db_fieldnames, new_entry)
    else:
        print("adding to db")
        add_entry(db, db_fieldnames, new_entry)

if __name__ == "__main__":
    main()