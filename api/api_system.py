import subprocess
import threading
import time

from flask import jsonify, request

from meshsrv import network_config
from system_log import get_system_events, log_system_event
from utils.helpers import get_device_model


MESHCenter_SERVICE = "meshcenter.service"


def register_system_routes(app, get_cpu_temperature=None, get_app_version=None):

    @app.route("/api/system/log")
    def api_system_log():
        limit = request.args.get("limit", 100)
        level = (request.args.get("level") or "").strip() or None
        source = (request.args.get("source") or "").strip() or None

        return jsonify({
            "ok": True,
            "events": get_system_events(limit=limit, level=level, source=source),
        })

    def execute_system_action(action):
        commands = {
            "restart_meshcenter": ["sudo", "-n", "/usr/bin/systemctl", "restart", MESHCenter_SERVICE],
            "reboot": ["sudo", "-n", "/usr/bin/systemctl", "reboot"],
            "shutdown": ["sudo", "-n", "/usr/bin/systemctl", "poweroff"],
        }
        labels = {
            "restart_meshcenter": "MeshCenter restart",
            "reboot": "Device reboot",
            "shutdown": "Device shutdown",
        }

        command = commands[action]
        label = labels[action]
        time.sleep(1.0)

        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
        except Exception as exc:
            log_system_event(
                f"{label} failed",
                "ERROR",
                str(exc),
                source="system",
            )

    @app.route("/api/system/action", methods=["POST"])
    def api_system_action():
        data = request.get_json(silent=True, force=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400
        action = str(data.get("action") or "").strip()

        allowed = {
            "restart_meshcenter": "MeshCenter restart requested",
            "reboot": "Device reboot requested",
            "shutdown": "Device shutdown requested",
        }

        if action not in allowed:
            return jsonify({"ok": False, "error": "Unsupported system action"}), 400

        device_model = get_device_model()
        detail = "Requested from MeshCenter web interface"
        if device_model:
            detail += f" ({device_model})"

        log_system_event(
            allowed[action],
            "ACTION",
            detail,
            source="system",
        )

        threading.Thread(
            target=execute_system_action,
            args=(action,),
            daemon=True,
        ).start()

        return jsonify({
            "ok": True,
            "accepted": True,
            "action": action,
            "message": allowed[action],
        }), 202

    @app.route("/api/system/info")
    def api_system_info():
        import os
        import shutil
        import platform

        result = {
            "hostname": None,
            "uptime": None,
            "cpu_temp": None,
            "load_avg": None,
            "ram_total_mb": None,
            "ram_used_mb": None,
            "ram_free_mb": None,
            "disk_total_gb": None,
            "disk_used_gb": None,
            "disk_free_gb": None,
            "model": None,
            "os": None,
            "kernel": platform.release(),
            "app_version": get_app_version() if get_app_version else None,
        }

        try:
            result["hostname"] = platform.node()
        except Exception:
            pass

        try:
            with open("/proc/uptime", "r") as f:
                seconds = int(float(f.read().split()[0]))
                days = seconds // 86400
                hours = (seconds % 86400) // 3600
                minutes = (seconds % 3600) // 60
                result["uptime"] = f"{days}d {hours}h {minutes}m"
        except Exception:
            pass

        # Not a second independent sensor read - shares the same source
        # server.py's e-paper System Screen uses (_read_cpu_temperature()),
        # passed in via get_cpu_temperature so both surfaces show the same
        # number instead of two code paths that happen to read the same
        # file and could silently drift apart (as they did before this
        # was unified - see the e-paper System Screen fix this shipped
        # alongside).
        if get_cpu_temperature is not None:
            try:
                result["cpu_temp"] = get_cpu_temperature()
            except Exception:
                pass

        try:
            result["load_avg"] = os.getloadavg()[0]
        except Exception:
            pass

        try:
            mem = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    key, value = line.split(":", 1)
                    mem[key] = int(value.strip().split()[0])

            total = mem.get("MemTotal", 0)
            available = mem.get("MemAvailable", 0)
            used = total - available

            result["ram_total_mb"] = round(total / 1024)
            result["ram_used_mb"] = round(used / 1024)
            result["ram_free_mb"] = round(available / 1024)
        except Exception:
            pass

        try:
            disk = shutil.disk_usage("/")
            result["disk_total_gb"] = round(disk.total / (1024 ** 3), 1)
            result["disk_used_gb"] = round(disk.used / (1024 ** 3), 1)
            result["disk_free_gb"] = round(disk.free / (1024 ** 3), 1)
        except Exception:
            pass

        model = get_device_model()
        result["model"] = model or None

        try:
            with open("/etc/os-release", "r") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        result["os"] = line.split("=", 1)[1].strip().strip('"')
                        break
        except Exception:
            pass

        return jsonify(result)

    @app.route("/api/system/top-processes")
    def api_system_top_processes():
        """On-demand top-5 CPU consumers, system-wide (not just MeshCenter).

        Stateless by design - no cross-request Process cache - so there's
        nothing to prune when a process dies and no memory growth over time.
        psutil.Process.cpu_percent(None) returns 0.0 on its first call per
        Process object (no prior sample to diff against), so every call here
        primes all handles, sleeps briefly, then samples again - the whole
        cost is one blocking sleep inside this single request, not a
        background loop, matching the "diagnostics you look at occasionally"
        budget this endpoint is for (see the frontend's on-open-only polling).
        """
        import psutil

        SAMPLE_WINDOW_S = 0.35

        try:
            procs = list(psutil.process_iter(["pid", "name"]))

            for proc in procs:
                try:
                    proc.cpu_percent(None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            time.sleep(SAMPLE_WINDOW_S)

            results = []
            for proc in procs:
                try:
                    cpu = proc.cpu_percent(None)
                    name = proc.info.get("name") or "?"
                    results.append({
                        "pid": proc.pid,
                        "name": name,
                        "cpu_percent": round(cpu, 1),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            results.sort(key=lambda item: item["cpu_percent"], reverse=True)

            return jsonify({"ok": True, "processes": results[:5]})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    def get_saved_wifi_names():
        result = network_config.list_wifi_connections()
        return result.get("ssids", set()) if result.get("ok") else set()

    @app.route("/api/system/wifi/scan")
    def api_system_wifi_scan():
        result = {"ok": True, "networks": []}
        try:
            scan_result = network_config.scan()
            if not scan_result.get("ok"):
                reason = scan_result.get("reason", "scan failed")
                log_system_event("Wi-Fi scan failed", "ERROR", reason, source="wifi")
                return jsonify({"ok": False, "error": reason, "networks": []}), 500
            out = scan_result.get("stdout", "")
            networks = []
            current = None
            for raw_line in out.splitlines():
                line = raw_line.strip()
                if line.startswith("BSS "):
                    if current and current.get("ssid"):
                        networks.append(current)
                    bssid = line.split()[1].split("(")[0]
                    current = {"ssid": None, "bssid": bssid, "signal_dbm": None, "signal": None, "frequency": None, "channel": None, "security": "Open", "connected": False}
                elif current is not None and line.startswith("SSID:"):
                    current["ssid"] = line.replace("SSID:", "").strip()
                elif current is not None and line.startswith("signal:"):
                    try:
                        dbm = float(line.replace("signal:", "").replace("dBm", "").strip())
                        current["signal_dbm"] = round(dbm, 1)
                        current["signal"] = max(0, min(100, int(2 * (dbm + 100))))
                    except Exception:
                        pass
                elif current is not None and line.startswith("freq:"):
                    try:
                        freq = int(line.replace("freq:", "").strip())
                        current["frequency"] = freq
                        if 2412 <= freq <= 2484:
                            current["band"] = "2.4 GHz"
                            current["channel"] = int((freq - 2407) / 5)
                        elif 5000 <= freq <= 5900:
                            current["band"] = "5 GHz"
                            current["channel"] = int((freq - 5000) / 5)
                        else:
                            current["band"] = "--"
                    except Exception:
                        pass
                elif current is not None:
                    if "RSN:" in line:
                        current["security"] = "WPA2/WPA3"
                    elif "WPA:" in line and current["security"] == "Open":
                        current["security"] = "WPA"
            if current and current.get("ssid"):
                networks.append(current)
            try:
                link = subprocess.check_output(["/usr/sbin/iw", "dev", "wlan0", "link"], text=True)
                connected_ssid = None
                connected_bssid = None
                for line in link.splitlines():
                    line = line.strip()
                    if line.startswith("Connected to "):
                        connected_bssid = line.split()[2].lower()
                    elif line.startswith("SSID:"):
                        connected_ssid = line.replace("SSID:", "").strip()
                for net in networks:
                    if (connected_bssid and net.get("bssid", "").lower() == connected_bssid) or (connected_ssid and net.get("ssid") == connected_ssid):
                        net["connected"] = True
            except Exception:
                pass
            by_ssid = {}
            for net in networks:
                ssid = net.get("ssid")
                if not ssid:
                    continue
                old = by_ssid.get(ssid)
                if old is None or (net.get("signal") or 0) > (old.get("signal") or 0):
                    by_ssid[ssid] = net
            saved_wifi = get_saved_wifi_names()
            result["networks"] = list(by_ssid.values())
            for net in result["networks"]:
                net["saved"] = net.get("ssid") in saved_wifi
            result["networks"].sort(key=lambda n: (not n.get("connected", False), -(n.get("signal") or 0)))
        except Exception as e:
            log_system_event("Wi-Fi scan failed", "ERROR", str(e), source="wifi")
            return jsonify({"ok": False, "error": str(e), "networks": []}), 500
        return jsonify(result)

    @app.route("/api/system/wifi/connect", methods=["POST"])
    def api_system_wifi_connect():
        data = request.get_json(silent=True, force=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400
        ssid = (data.get("ssid") or "").strip()
        password = data.get("password") or ""
        if not ssid:
            return jsonify({"ok": False, "error": "SSID is required"}), 400
        # meshsrv.network_config.connect() sends `password` to the helper's
        # stdin, never as an argv element (P1 #8) - and the helper itself
        # unconditionally replaces any existing profile for this SSID
        # before writing a fresh one, so the old two-pass
        # delete-then-retry dance that used to live here (for "key-mgmt
        # property is missing" errors) is no longer needed: a freshly
        # written profile can't inherit a stale/mismatched security config.
        result = network_config.connect(ssid, password)
        if result.get("ok"):
            return jsonify({"ok": True, "message": result.get("stdout", "")})
        return jsonify({"ok": False, "error": result.get("reason", "connect failed")}), 500

    @app.route("/api/system/wifi/forget", methods=["POST"])
    def api_system_wifi_forget():
        data = request.get_json(silent=True, force=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400
        ssid = (data.get("ssid") or "").strip()
        if not ssid:
            return jsonify({"ok": False, "error": "SSID is required"}), 400
        result = network_config.forget(ssid)
        if result.get("ok"):
            return jsonify({"ok": True, "message": result.get("stdout", "")})
        return jsonify({"ok": False, "error": result.get("reason", "forget failed")}), 500

    @app.route("/api/system/network")
    def api_system_network():
        result = {"ssid": None, "signal_percent": None, "rssi_dbm": None, "ip": None, "gateway": None, "internet": False}
        try:
            result["ssid"] = subprocess.check_output(["iwgetid", "-r"], text=True).strip()
        except Exception:
            pass
        try:
            # Unprivileged - iw link status needs no root on the target
            # systems, so this stays a direct call, not routed through the
            # sudo-gated helper (see meshsrv/network_config.py's docstring).
            out = subprocess.check_output(["/usr/sbin/iw", "dev", "wlan0", "link"], text=True)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    result["ssid"] = line.replace("SSID:", "").strip()
                elif line.startswith("signal:"):
                    dbm = int(line.replace("signal:", "").replace("dBm", "").strip())
                    result["rssi_dbm"] = dbm
                    result["signal_percent"] = max(0, min(100, 2 * (dbm + 100)))
                elif line.startswith("rx bitrate:"):
                    result["rx_bitrate"] = line.replace("rx bitrate:", "").strip()
                elif line.startswith("tx bitrate:"):
                    result["tx_bitrate"] = line.replace("tx bitrate:", "").strip()
        except Exception:
            pass
        try:
            ip = subprocess.check_output(["hostname", "-I"], text=True).strip().split()
            if ip:
                result["ip"] = ip[0]
        except Exception:
            pass
        try:
            route = subprocess.check_output(["ip", "route"], text=True)
            for line in route.splitlines():
                if line.startswith("default"):
                    result["gateway"] = line.split()[2]
        except Exception:
            pass
        try:
            subprocess.check_call(["ping", "-c", "1", "-W", "1", "8.8.8.8"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            result["internet"] = True
        except Exception:
            pass
        return jsonify(result)

    @app.route("/api/time")
    def api_get_time():
        from meshsrv.time_service import get_status

        return jsonify(get_status())

    @app.route("/api/notifications", methods=["GET"])
    def api_get_notifications():
        from meshsrv.notification_service import get_all, get_unread_count

        return jsonify({"notifications": get_all(), "unread_count": get_unread_count()})

    @app.route("/api/notifications/test", methods=["POST"])
    def api_test_notification():
        from meshsrv.notification_service import push_notification

        event = push_notification(
            level="info", source="system",
            title="Notification test", body="The notifications card is working correctly"
        )
        return jsonify({"ok": True, "event": event})

    @app.route("/api/notifications/read-all", methods=["POST"])
    def api_mark_all_notifications_read():
        from meshsrv.notification_service import mark_all_read

        count = mark_all_read()
        return jsonify({"marked": count})

    @app.route("/api/notifications/<nid>/read", methods=["PATCH"])
    def api_mark_notification_read(nid):
        from meshsrv.notification_service import mark_read

        ok = mark_read(nid)
        return jsonify({"ok": ok})

    @app.route("/api/notifications/<nid>", methods=["DELETE"])
    def api_delete_notification(nid):
        from meshsrv.notification_service import delete_one

        ok = delete_one(nid)
        return jsonify({"ok": ok})

    @app.route("/api/notifications", methods=["DELETE"])
    def api_clear_notifications():
        from meshsrv.notification_service import clear_all

        count = clear_all()
        return jsonify({"cleared": count})

    @app.route("/api/schedules", methods=["GET"])
    def api_get_schedules():
        from meshsrv.schedule_engine import get_all_rules

        return jsonify(get_all_rules())

    @app.route("/api/schedules", methods=["POST"])
    def api_create_schedule():
        from meshsrv.schedule_engine import create_rule

        data = request.get_json(silent=True, force=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400
        return jsonify(create_rule(data)), 201

    @app.route("/api/schedules/<sid>", methods=["PUT"])
    def api_update_schedule(sid):
        from meshsrv.schedule_engine import update_rule

        data = request.get_json(silent=True, force=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400
        result = update_rule(sid, data)
        if result is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(result)

    @app.route("/api/schedules/<sid>/toggle", methods=["PATCH"])
    def api_toggle_schedule(sid):
        from meshsrv.schedule_engine import toggle_rule

        result = toggle_rule(sid)
        if result is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(result)

    @app.route("/api/schedules/<sid>", methods=["DELETE"])
    def api_delete_schedule(sid):
        from meshsrv.schedule_engine import delete_rule

        ok = delete_rule(sid)
        return jsonify({"ok": ok})

    @app.route("/api/timers", methods=["GET"])
    def api_get_timers():
        from meshsrv.timer_service import get_all

        return jsonify(get_all())

    @app.route("/api/timers", methods=["POST"])
    def api_create_timer():
        from meshsrv.timer_service import create_timer

        data = request.get_json(silent=True, force=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400
        label = data.get("label", "")
        duration_s = data.get("duration_s")
        notify_cfg = data.get("notify")
        t = create_timer(label, duration_s, notify_cfg)
        return jsonify(t), 201

    @app.route("/api/timers/<tid>/pause", methods=["PATCH"])
    def api_pause_timer(tid):
        from meshsrv.timer_service import pause_timer

        t = pause_timer(tid)
        if t is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(t)

    @app.route("/api/timers/<tid>/resume", methods=["PATCH"])
    def api_resume_timer(tid):
        from meshsrv.timer_service import resume_timer

        t = resume_timer(tid)
        if t is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(t)

    @app.route("/api/timers/<tid>/stop", methods=["PATCH"])
    def api_stop_timer(tid):
        from meshsrv.timer_service import stop_timer

        t = stop_timer(tid)
        if t is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(t)

    @app.route("/api/timers/<tid>/reset", methods=["PATCH"])
    def api_reset_timer(tid):
        from meshsrv.timer_service import reset_timer

        t = reset_timer(tid)
        if t is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(t)

    @app.route("/api/timers/<tid>/finish", methods=["POST"])
    def api_finish_timer(tid):
        from meshsrv.timer_service import mark_finished
        from meshsrv.notification_service import push_notification

        t = mark_finished(tid)
        if t is None:
            return jsonify({"error": "not found"}), 404

        label = t.get("label") or "Timer"
        push_notification(level="info", source="timer", title=label, body="Timer finished")

        notify = t.get("notify", {})
        m = notify.get("mesh_message", {})
        if notify.get("enabled") and m.get("enabled"):
            signal = (notify.get("signal") or "").strip()
            if signal:
                try:
                    from meshsrv.schedule_actions import send_mesh_message

                    send_mesh_message(
                        message=signal,
                        target_type=m.get("target_type", "node"),
                        node_id=m.get("node_id", ""),
                        channel_index=m.get("channel_index", 0)
                    )
                except Exception as e:
                    print(f"[Timer] mesh send failed: {e}", flush=True)

        return jsonify(t)

    @app.route("/api/timers/<tid>", methods=["DELETE"])
    def api_delete_timer(tid):
        from meshsrv.timer_service import delete_timer

        ok = delete_timer(tid)
        return jsonify({"ok": ok})
