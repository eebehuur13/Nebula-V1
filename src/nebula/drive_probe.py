"""Read-only Drive checks for the open questions, run where Nebula runs (`nebula probe-drive`).

It answers, against the real service account:
1. What role does the service account have on a shared folder (Viewer or Editor), and can it still
   read each file's sharing? (`permissionIds` on listings, and permissions.list per file.)
2. What does discovery see as shared with Nebula (folders, files, Shared Drives) and who shared them?
3. With `watch` seconds: does the service account's change feed report edits to files in a folder
   shared with it? Edit a doc in the folder while it waits.

Nothing is registered, shared or written, and no tokens or file contents are printed.
"""

import asyncio
import json
import time

from nebula.connectors import gdrive


def say(**data):
    print(json.dumps(data, default=str), flush=True)


async def main(folder_link: str, watch: int = 0) -> None:
    folder = gdrive.drive_id_from(folder_link)
    connector = gdrive.GoogleDriveConnector({"roots": [{"id": folder, "kind": "folder"}]})
    try:
        root = await connector._get_json(f"/files/{folder}", {
            "supportsAllDrives": "true",
            "fields": "id,name,mimeType,permissionIds,capabilities(canEdit,canShare,canListChildren,canDownload)"})
        caps = root.get("capabilities") or {}
        say(check="role on the folder", folder=root.get("name"), role="Editor" if caps.get("canEdit") else "Viewer",
            capabilities=caps, folder_permission_ids_visible="permissionIds" in root)

        files, _ = await connector._list_files(gdrive._children_query(folder), None)
        files = [f for f in files if f.get("mimeType") != gdrive.FOLDER][:5]
        for f in files:
            audience = await connector.file_audience(f["id"])
            say(check="file sharing", file=f.get("name"), permission_ids_on_listing="permissionIds" in f,
                permissions_list="unreadable" if audience["unknown"] else "readable",
                people=len(audience["emails"]), groups=len(audience["groups"]), domains=audience["domains"])

        found = await connector.shared_with_nebula()
        say(check="discovery", shared_with_nebula=len(found),
            items=[{"name": x["name"], "kind": x["kind"], "shared_by": x["shared_by"] or None} for x in found[:20]])

        wait = watch
        if wait:
            token = await connector._start_token()
            say(check="change feed", waiting_seconds=wait, note="edit a file in the folder now")
            deadline, seen = time.time() + wait, []
            while time.time() < deadline and not seen:
                await asyncio.sleep(15)
                data = await connector._get_json("/changes", {
                    "pageToken": token, "pageSize": 100, "includeRemoved": "true", **gdrive.ALL_DRIVES,
                    "fields": "nextPageToken,newStartPageToken,changes(fileId,file(name,parents))"}) or {}
                seen = [c for c in data.get("changes", []) if c.get("file")]
                token = data.get("newStartPageToken") or data.get("nextPageToken") or token
            say(check="change feed", edits_reported=bool(seen), files=[c["file"].get("name") for c in seen[:10]])
    finally:
        await connector.close()

