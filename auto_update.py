import csv, requests, os
from typing import Dict, List, Tuple

gh_s = requests.Session()
gh_token = os.getenv("GITHUB_TOKEN")
gh_s.headers.update({
    "authorization": "Bearer "+gh_token
})


def error_log(text: str):
    print(text)

def log(text: str):
    print(text)


filter_release_assets = lambda release, file_end: [item for item in release["assets"] if item['name'].endswith(file_end)]

def github_find_asset(releases: List[Dict], file_type: str, old_filename: str) -> Tuple[Dict, Dict]:
    for release in releases:
        files = filter_release_assets(release, file_type)
        # if its only 1 file with right file type use it
        if len(files) == 1:
            return files[0], release

        # special case for julius
        zips = filter_release_assets(release, "vita.zip")
        if len(zips):
            return zips[0], release

        # try to find the one with same name
        elif len(files) > 1:
            same_name = [file for file in files if file['name'] == old_filename]
            if len(same_name):
                return same_name[0], release
            # more than 1 file and none with right end or same name
            return None, None
    return None, None


class update_error(Exception):
    pass

# todo remove spaghetti
def check_update_github(row):
    try:
        owner, repo = row["download_url"].split("/")[3:5]
        old_filename = row["download_url"].split("/")[-1]
        repo_url = f"https://github.com/{owner}/{repo}"

        # get releases
        resp = gh_s.get(f"https://api.github.com/repos/{owner}/{repo}/releases")
        if resp.status_code == 404:
            raise update_error(f"repo not found {repo_url}")
        elif resp.status_code != 200:
            raise Exception(resp.text)
        releases = resp.json()
        if len(releases) == 0:
            raise update_error(f"repo has no releases {repo_url}")

        # expected file type
        if row["type"] == "VPK":
            file_type = ".vpk"
        elif row["type"] == "PLUGIN":
            file_type = "prx"
        elif row["type"] == "DATA":
            file_type = ""
        else:
            raise update_error(f"csv invalid entry ({row['id']})")

        # find what file and release to use
        file, release = github_find_asset(releases, file_type, old_filename)
        if file == None:
            raise update_error(f"repo has no recognized files {repo_url}")
        
        # update row if new
        new_url = file["browser_download_url"]
        if row["download_url"] != new_url:
            log(f"updated {row['title']}   to: {release['tag_name']}")
            row["download_url"] = new_url

    except update_error as e:
        error_log(str(e))
    return row


def check_update(row):
    if row["download_url"].count("github.com/"):
        return check_update_github(row)
    return row


# read db
f = open("cbpsdb.csv", "r", encoding="utf8")
field_names = f.readline()[:-1].split(",")
db = csv.DictReader(f.read().splitlines(False), field_names)
f.close()

# open writer
f = open("cbpsdb.csv", "w", encoding="utf8", newline="")
db_writer = csv.DictWriter(f, field_names)
db_writer.writeheader()

# check for updates on all entries
updated = []
for row in db:
    row_orig = dict(row)
    check_update(row)
    if row != row_orig:
        updated.append((row, row_orig))
    db_writer.writerow(row)
f.close()

# tell gh action to pull request
if len(updated):
    # write pull message
    updated_str = "updating: " + ", ".join([row[0]['title'] for row in updated])
    print(updated_str)
    print(f'::set-output name=updated::{updated_str}')

    # write list of urls that changed
    open("updated-urls.csv", "w").write("\n".join([f'{row[1]["download_url"]},{row[0]["download_url"]}' for row in updated]))
    
    #get all open pull requests on this repo
    pulls = gh_s.get(f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}/pulls?state=open").json()
    # only send pull if theres none open by the bot
    if not len([pull for pull in pulls if pull['user']['login'] == "github-actions[bot]"]):
        print(f'::set-output name=has_open_pulls::true')