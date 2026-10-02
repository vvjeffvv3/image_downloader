"""Retry and inspection operate on saved YouTube records, without scanning a channel."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from downloader import (
    Cancelled, DownloadError, Report, YouTubeClient, full_size_url, read_json,
    save_image, saved_image_path, write_atomic, write_json,
)
from image_checks import inspect_image, require_pillow


def metadata_files(output, scan_root=None):
    scan_root = Path(scan_root).resolve() if scan_root is not None else output
    for path in sorted(scan_root.rglob("post.json")):
        if path.resolve().is_relative_to(scan_root.resolve()):
            yield path


def photo_candidates(item):
    urls = item.get("candidates") or [item.get("source_url", ""), item.get("download_url", "")]
    if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
        raise DownloadError("저장된 사진 주소가 올바르지 않습니다.")
    return list(dict.fromkeys(url for url in urls if url))


def read_photo(folder, item):
    path = saved_image_path(folder, item.get("file", ""))
    if not path.is_file():
        raise DownloadError("사진 파일이 없습니다.")
    if path.stat().st_size > 100 * 1024 * 1024:
        raise DownloadError("사진이 허용 크기(100 MiB)를 초과했습니다.")
    content = path.read_bytes()
    details = inspect_image(content)
    if item.get("sha256") and item["sha256"] != details["sha256"]:
        raise DownloadError("저장 당시와 파일 내용이 달라졌습니다.")
    return path, details


def backup_photo(folder, item):
    if not item.get("file"):
        return None
    path = saved_image_path(folder, item["file"])
    if not path.is_file():
        return None
    if path.stat().st_size > 100 * 1024 * 1024:
        raise DownloadError("기존 사진이 너무 커서 백업할 수 없습니다.")
    content = path.read_bytes()
    backup_dir = folder / "backups"
    if backup_dir.resolve().parent != folder.resolve():
        raise DownloadError("백업 폴더 위치가 올바르지 않습니다.")
    backup_dir.mkdir(exist_ok=True)
    name = datetime.now().strftime("%Y%m%d_%H%M%S_%f_") + path.name
    write_atomic(backup_dir / name, content)
    return "backups/" + name


def finish_report(output, report, log, **extra):
    path = output / f"{report.operation}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    report.manifest = str(path)
    write_json(path, {"platform": "youtube", **extra, **asdict(report)})
    log(report.summary())
    return report


def retry_failed(output: Path, stop=None, log=print, interval=1.0, control=None, client=None, scan_root=None):
    output = output.expanduser().resolve()
    if scan_root is not None and Path(scan_root).is_dir():
        output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        raise DownloadError("저장된 사진이 있는 유튜브 폴더를 선택해 주세요.")
    require_pillow()
    client = client or YouTubeClient(stop, interval=interval, control=control)
    report = Report(operation="retry")
    try:
        for metadata_path in metadata_files(output, scan_root):
            client.check_cancelled()
            try:
                data = read_json(metadata_path)
                if data.get("platform", "youtube") != "youtube":
                    continue
                touched = False
                for index, item in enumerate(data.get("images", []), 1):
                    client.check_cancelled()
                    missing = not item.get("file") or not saved_image_path(metadata_path.parent, item["file"]).is_file()
                    if item.get("status") not in {"failed", "cancelled", "pending"} and not missing:
                        report.images_skipped += 1
                        continue
                    if not missing and item.get("status") in {"cancelled", "pending"}:
                        try:
                            _, details = read_photo(metadata_path.parent, item)
                        except (DownloadError, OSError):
                            pass
                        else:
                            item.update(details, status="skipped")
                            item.pop("error", None)
                            report.images_skipped += 1
                            write_json(metadata_path, data)
                            log(f"  사진 {index}: 이미 받은 정상 사진은 건너뜁니다.")
                            continue
                    if not touched:
                        report.posts += 1
                        report.folders.append(str(metadata_path.parent))
                        touched = True
                        log(f"재시도: {data.get('author', '')} · {data.get('post_id', '')}")
                    try:
                        old_filename = item.get("file")
                        backup = backup_photo(metadata_path.parent, item)
                        outcome = save_image(client, photo_candidates(item), metadata_path.parent,
                                             index, previous=item, force=True)
                        item.update(outcome)
                        if backup:
                            item["backup_file"] = backup
                            if old_filename != outcome["file"]:
                                saved_image_path(metadata_path.parent, old_filename).unlink(missing_ok=True)
                        item.pop("error", None)
                        item.pop("verification", None)
                        report.images_saved += 1
                        log(f"  사진 {index}: 다시 저장했습니다.")
                    except (Cancelled, KeyboardInterrupt):
                        item["status"] = "cancelled"
                        raise
                    except (DownloadError, OSError) as error:
                        item.update(status="failed", error=str(error))
                        report.images_failed += 1
                        log(f"  사진 {index}: 실패 — {error}")
                    finally:
                        data["retried_at"] = datetime.now(timezone.utc).isoformat()
                        write_json(metadata_path, data)
            except Cancelled:
                raise
            except (DownloadError, OSError, TypeError, AttributeError) as error:
                report.errors.append(f"{metadata_path.parent.name}: {error}")
                log(f"저장 기록 오류: {metadata_path.parent.name} — {error}")
    except (Cancelled, KeyboardInterrupt):
        report.cancelled = True
        log("재시도를 중지했습니다. 저장한 사진은 유지됩니다.")
    finally:
        finish_report(output, report, log)
    if not report.posts and not report.errors and not report.cancelled:
        log("다시 받을 실패·중지·누락 사진이 없습니다.")
    return report


def inspect_saved(output: Path, check_original=True, repair=False, stop=None,
                  log=print, interval=1.0, control=None, client=None, scan_root=None):
    output = output.expanduser().resolve()
    if scan_root is not None and Path(scan_root).is_dir():
        output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        raise DownloadError("검사할 사진이 있는 유튜브 폴더를 선택해 주세요.")
    require_pillow()
    client = client or YouTubeClient(stop, interval=interval, control=control)
    report = Report(operation="inspect")
    try:
        for metadata_path in metadata_files(output, scan_root):
            client.check_cancelled()
            try:
                data = read_json(metadata_path)
                if data.get("platform", "youtube") != "youtube" or not data.get("images"):
                    continue
                report.posts += 1
                report.folders.append(str(metadata_path.parent))
                log(f"사진 검사: {data.get('author', '')} · {data.get('post_id', '')}")
                for index, item in enumerate(data["images"], 1):
                    client.check_cancelled()
                    report.images_checked += 1
                    verification = {"checked_at": datetime.now(timezone.utc).isoformat(),
                                    "integrity": "ok", "original": "not_checked"}
                    local = None
                    damaged = different = repaired = False
                    try:
                        try:
                            _, local = read_photo(metadata_path.parent, item)
                            verification["local"] = local
                            # Earlier app versions did not record a hash. A successful
                            # decode provides a baseline without claiming original quality.
                            if not item.get("sha256"):
                                item.update(local)
                        except (DownloadError, OSError) as error:
                            damaged = True
                            report.images_damaged += 1
                            verification.update(integrity="failed", integrity_error=str(error))
                            item.update(status="failed", error=str(error))
                            log(f"  사진 {index}: 손상 또는 누락 — {error}")
                        candidates = photo_candidates(item)
                        original = next((url for url in candidates if full_size_url(url)), None)
                        prefetched = {}
                        if check_original:
                            if not original:
                                verification.update(original="unknown", original_error="최대 크기 주소가 없습니다.")
                                report.images_original_unknown += 1
                            else:
                                try:
                                    content = client.request(client.validate_image_url(original), max_bytes=100 * 1024 * 1024)
                                    remote = inspect_image(content)
                                    prefetched[original] = content
                                    verification.update(original_url=original, server=remote)
                                    if local and local["sha256"] == remote["sha256"]:
                                        verification["original"] = "match"
                                        item["quality"] = "full"
                                        report.images_original += 1
                                        log(f"  사진 {index}: 서버 최대 크기 파일과 일치합니다.")
                                    else:
                                        different = True
                                        report.images_different += 1
                                        verification["original"] = "smaller" if local and (
                                            local["width"] < remote["width"] or local["height"] < remote["height"]
                                        ) else "different"
                                        log(f"  사진 {index}: 서버 최대 크기 파일과 차이가 있습니다.")
                                except Cancelled:
                                    raise
                                except DownloadError as error:
                                    verification.update(original="unknown", original_error=str(error))
                                    report.images_original_unknown += 1
                                    log(f"  사진 {index}: 최대 크기 대조 불가 — {error}")
                        if repair and (damaged or different):
                            try:
                                old_filename = item.get("file")
                                backup = backup_photo(metadata_path.parent, item)
                                # If a different original was already fetched, require that
                                # original instead of silently replacing with a smaller variant.
                                repair_candidates = [original] if different else candidates
                                outcome = save_image(client, repair_candidates, metadata_path.parent,
                                                     index, previous=item, force=True, prefetched=prefetched)
                                item.update(outcome)
                                item.pop("error", None)
                                if backup:
                                    verification["backup_file"] = backup
                                    if old_filename != outcome["file"]:
                                        saved_image_path(metadata_path.parent, old_filename).unlink(missing_ok=True)
                                verification["integrity"] = "ok"
                                verification["repaired"] = True
                                verification["local_after_repair"] = {key: outcome[key] for key in ("sha256", "width", "height", "format", "frames")}
                                if full_size_url(outcome["download_url"]):
                                    verification["original"] = "match"
                                    if different:
                                        report.images_original += 1
                                report.images_repaired += 1
                                report.images_saved += 1
                                repaired = True
                                log(f"  사진 {index}: 복구했습니다. 기존 파일은 백업 폴더에 보관합니다.")
                            except Cancelled:
                                raise
                            except (DownloadError, OSError) as error:
                                verification["repair_error"] = str(error)
                                report.errors.append(f"{metadata_path.parent.name} 사진 {index}: {error}")
                                log(f"  사진 {index}: 복구 실패 — {error}")
                        if damaged and not repaired:
                            report.images_failed += 1
                        if not damaged and not check_original:
                            log(f"  사진 {index}: 파일 정상 ({local['width']} × {local['height']})")
                    finally:
                        item["verification"] = verification
                        write_json(metadata_path, data)
            except Cancelled:
                raise
            except (DownloadError, OSError, TypeError, AttributeError) as error:
                report.errors.append(f"{metadata_path.parent.name}: {error}")
                log(f"저장 기록 오류: {metadata_path.parent.name} — {error}")
    except (Cancelled, KeyboardInterrupt):
        report.cancelled = True
        log("검사를 중지했습니다. 기존 사진은 유지됩니다.")
    finally:
        finish_report(output, report, log, check_original=check_original, repair=repair)
    if not report.posts and not report.errors and not report.cancelled:
        log("검사할 저장 사진이 없습니다.")
    return report
