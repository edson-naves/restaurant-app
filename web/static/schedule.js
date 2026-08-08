/* Schedule drag-and-drop (pointer-based).
 *
 * Managers schedule by dragging: drop a teammate from the Team panel onto a day
 * to create a shift, drag a shift block to move it, and drag a block's bottom
 * edge to resize. Uses pointer events (not native HTML5 drag) so there's a live
 * ghost, a highlighted target column, and a precise drop — and no fight with the
 * block's click-to-open. Every gesture posts to the create/edit routes and
 * reloads, so there's no client state to drift. Non-managers can't drag.
 */
(function () {
  "use strict";

  // Team search (used by the input's oninput=).
  window.filterTeam = function (q) {
    q = (q || "").toLowerCase();
    document.querySelectorAll(".team-row").forEach(function (r) {
      r.style.display = r.dataset.name.indexOf(q) === -1 ? "none" : "";
    });
  };

  var body = document.querySelector(".cal-body");
  if (!body) return;

  var PPH = 44;                       // pixels per hour (matches the CSS grid)
  var SNAP = 5;                       // snap dropped/resized times to 5 minutes
  var DEFAULT_MIN = 240;              // a fresh dragged-in shift is 4h
  var THRESH = 4;                     // px of movement before a press becomes a drag
  var startHour = parseInt(body.dataset.startHour || "8", 10);
  var canManage = body.dataset.manage === "1";

  function minuteFromY(col, clientY) {
    var rect = col.getBoundingClientRect();
    var min = startHour * 60 + ((clientY - rect.top) / PPH) * 60;
    return Math.max(startHour * 60, Math.round(min / SNAP) * SNAP);
  }
  function hhmm(min) {
    min = ((Math.round(min) % 1440) + 1440) % 1440;
    var h = Math.floor(min / 60), m = min % 60;
    return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
  }
  function post(url, data, forced) {
    var payload = Object.assign({}, data);
    if (forced) payload.force = 1;
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
      body: new URLSearchParams(payload).toString(),
    }).then(function (r) {
      // 409 = a soft conflict (e.g. approved time off) — ask, then retry forced.
      if (r.status === 409 && !forced) {
        return r.json().then(function (j) {
          if (window.confirm(j.detail || "This conflicts. Proceed anyway?")) post(url, data, true);
        }).catch(function () { location.reload(); });
      }
      location.reload();
    }).catch(function () { location.reload(); });
  }
  function columnAt(x, y) {
    var el = document.elementFromPoint(x, y);
    return el ? el.closest(".cal-col[data-date]") : null;
  }
  function clearOver() {
    document.querySelectorAll(".cal-col-over").forEach(function (c) {
      c.classList.remove("cal-col-over");
    });
  }

  // ---- Unified pointer drag (move a block, or create from a teammate) -----
  var drag = null;

  function beginDrag(type, source, e, data) {
    var ghost = document.createElement("div");
    ghost.className = "cal-ghost" + (type === "new" ? " new" : "");
    var w = 150, h = 40, dur = DEFAULT_MIN, name = data.name || "New shift";
    if (type === "move") {
      var r = source.getBoundingClientRect();
      w = r.width; h = r.height;
      dur = parseInt(source.dataset.duration || String(DEFAULT_MIN), 10);
      ghost.style.setProperty("--pc", getComputedStyle(source).getPropertyValue("--pc") || "#3b82f6");
      var nm = source.querySelector(".cb-name"); name = nm ? nm.textContent : "";
    } else {
      h = Math.max(30, DEFAULT_MIN / 60 * PPH);   // fresh shift looks like its 4h
    }
    ghost.style.width = w + "px";
    ghost.style.height = h + "px";
    ghost.innerHTML = '<b class="g-time"></b><span class="g-name"></span>';
    ghost.querySelector(".g-name").textContent = name;
    // Hold the middle of the block: cursor sits at the ghost's centre.
    drag = { type: type, source: source, data: data, w: w, h: h, dur: dur,
             startX: e.clientX, startY: e.clientY, moved: false, ghost: ghost };
    document.addEventListener("pointermove", onDragMove);
    document.addEventListener("pointerup", onDragUp);
  }

  function dropStart(col, clientY, h) {
    // The block's TOP is half its height above the cursor (centre-held).
    return minuteFromY(col, clientY - h / 2);
  }

  // Minutes past the column's midnight can exceed a day on an overnight window
  // (e.g. a 2 AM slot in a 07:00–04:00 view); roll onto the next date so the
  // shift is stored with a real datetime.
  function dropDateTime(colDateStr, absMin) {
    absMin = Math.round(absMin);
    var d = new Date(colDateStr + "T00:00:00");
    d.setDate(d.getDate() + Math.floor(absMin / 1440));
    var y = d.getFullYear(), m = ("0" + (d.getMonth() + 1)).slice(-2), day = ("0" + d.getDate()).slice(-2);
    return { date: y + "-" + m + "-" + day, time: hhmm(absMin) };
  }

  function onDragMove(e) {
    if (!drag) return;
    if (!drag.moved) {
      if (Math.abs(e.clientX - drag.startX) < THRESH && Math.abs(e.clientY - drag.startY) < THRESH) return;
      drag.moved = true;
      document.body.appendChild(drag.ghost);
      if (drag.type === "move") drag.source.style.visibility = "hidden";
    }
    drag.ghost.style.left = (e.clientX - drag.w / 2) + "px";
    drag.ghost.style.top = (e.clientY - drag.h / 2) + "px";
    clearOver();
    var col = columnAt(e.clientX, e.clientY);
    var gt = drag.ghost.querySelector(".g-time");
    if (col) {
      col.classList.add("cal-col-over");
      var s = dropStart(col, e.clientY, drag.h);
      gt.textContent = hhmm(s) + "–" + hhmm(s + drag.dur);   // live time, so you see where it lands
    } else {
      gt.textContent = "—";
    }
  }

  function onDragUp(e) {
    document.removeEventListener("pointermove", onDragMove);
    document.removeEventListener("pointerup", onDragUp);
    var d = drag; drag = null;
    clearOver();
    if (!d) return;
    if (d.ghost.parentNode) d.ghost.parentNode.removeChild(d.ghost);
    if (d.type === "move") d.source.style.visibility = "";
    if (!d.moved) return;               // a click, not a drag — leave it alone
    var col = columnAt(e.clientX, e.clientY);
    if (!col) return;
    var start = dropStart(col, e.clientY, d.h);
    var st = dropDateTime(col.dataset.date, start);
    if (d.type === "new") {
      post("/schedule/shifts", {
        staff_id: d.data.staff, position_id: 0, date: st.date,
        start: st.time, end: hhmm(start + d.dur), notes: "",
      });
    } else {
      var b = d.source;
      post("/schedule/shifts/" + b.dataset.shiftId + "/edit", {
        staff_id: b.dataset.staffId || "0", position_id: b.dataset.positionId || "0",
        date: st.date, start: st.time, end: hhmm(start + d.dur),
        notes: b.dataset.notes || "",
      });
    }
  }

  if (canManage) {
    document.querySelectorAll(".cblock").forEach(function (b) {
      var face = b.querySelector(".cb-face");
      if (face) face.addEventListener("pointerdown", function (e) {
        if (e.button === 0) beginDrag("move", b, e, {});
      });
    });
    document.querySelectorAll(".team-row[data-staff-id]").forEach(function (row) {
      row.addEventListener("pointerdown", function (e) {
        if (e.button !== 0) return;
        var nm = row.querySelector("b");
        beginDrag("new", row, e, { staff: row.dataset.staffId, name: nm ? nm.textContent : "" });
      });
    });
  }

  // ---- Resize by dragging a block's bottom edge --------------------------
  document.querySelectorAll(".cblock .cb-resize").forEach(function (handle) {
    handle.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var b = handle.closest(".cblock");
      var col = b.closest(".cal-col");
      var startMin = parseInt(b.dataset.startMin, 10);
      var endMin = startMin + parseInt(b.dataset.duration || "60", 10);
      b.classList.add("resizing");
      handle.setPointerCapture(e.pointerId);

      function onMove(ev) {
        endMin = minuteFromY(col, ev.clientY);
        if (endMin < startMin + SNAP) endMin = startMin + SNAP;
        b.style.height = ((endMin - startMin) / 60 * PPH) + "px";
        var t = b.querySelector(".cb-time");       // live time while resizing
        if (t) t.textContent = hhmm(startMin) + "–" + hhmm(endMin);
      }
      function onUp() {
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        post("/schedule/shifts/" + b.dataset.shiftId + "/edit", {
          staff_id: b.dataset.staffId || "0", position_id: b.dataset.positionId || "0",
          date: col.dataset.date, start: hhmm(startMin), end: hhmm(endMin),
          notes: b.dataset.notes || "",
        });
      }
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
    });
  });

  // ---- Extend across days: drag the right edge to repeat the shift --------
  document.querySelectorAll(".cblock .cb-extend").forEach(function (handle) {
    handle.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var b = handle.closest(".cblock");
      var srcCol = b.closest(".cal-col");
      var cols = Array.prototype.slice.call(document.querySelectorAll(".cal-col[data-date]"));
      var srcIdx = cols.indexOf(srcCol);
      var covered = [srcCol];
      handle.setPointerCapture(e.pointerId);

      function onMove(ev) {
        clearOver();
        covered = [srcCol];
        var tgt = columnAt(ev.clientX, ev.clientY);
        var ti = tgt ? cols.indexOf(tgt) : -1;
        if (ti >= 0) {
          covered = cols.slice(Math.min(srcIdx, ti), Math.max(srcIdx, ti) + 1);
        }
        covered.forEach(function (c) { c.classList.add("cal-col-over"); });
      }
      function onUp() {
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        clearOver();
        var dates = covered
          .filter(function (c) { return c !== srcCol; })
          .map(function (c) { return c.dataset.date; });
        if (dates.length) {
          post("/schedule/shifts/" + b.dataset.shiftId + "/repeat", { dates: dates.join(",") });
        }
      }
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
    });
  });

  // Request/clock actions post in the background and refresh the current view
  // in place — so approving a request updates the calendar without navigating
  // away or making you hit refresh.
  function sendForm(f, forced) {
    var act = f.getAttribute("action") || "";
    var flash = null, decided = false;
    if (act === "/schedule/timeoff") flash = "✓ Time-off request sent for approval";
    else if (act === "/schedule/propose") flash = "✓ Shift-change request sent for approval";
    else if (/\/shifts\/\d+\/swap$/.test(act)) flash = "✓ Swap request sent";
    else if (/\/(approve|deny)$/.test(act)) {
      decided = true;
      flash = /\/approve$/.test(act) ? "✓ Request approved" : "✕ Request denied";
    }
    var fd = new FormData(f);
    if (forced) fd.set("force", "1");
    fetch(f.action, { method: "POST", body: fd, headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (r.status === 409 && !forced) {
          return r.json().then(function (j) {
            if (window.confirm(j.detail || "This conflicts. Proceed anyway?")) sendForm(f, true);
          }).catch(function () { location.reload(); });
        }
        // A rejection (e.g. a duplicate time-off request) — show the reason and
        // leave the form as it is, rather than falsely flashing "sent".
        if (!r.ok) {
          return r.json().then(function (j) {
            var msg = (j && j.detail) ? j.detail : "That request couldn't be completed.";
            if (window.rmsToast) window.rmsToast("⚠ " + msg); else window.alert(msg);
          }).catch(function () { location.reload(); });
        }
        if (flash) { try { sessionStorage.setItem("rms-flash", flash); } catch (e2) {} }
        // Approve/deny redirects to the affected week — follow it so the manager
        // lands where the change is visible (the reassigned shift, the moved
        // shift, or the time-off band), rather than reloading the current week
        // where nothing appears to have changed.
        if (decided && r.redirected && r.url) location.href = r.url;
        else location.reload();
      })
      .catch(function () { location.reload(); });
  }
  document.querySelectorAll(
    'form[action^="/schedule/timeoff"], form[action^="/schedule/swaps"],'
    + ' form[action*="/swap"], form[action*="/clock-"], form.sched-add,'
    + ' form[action="/schedule/propose"]'
  ).forEach(function (f) {
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      sendForm(f, false);
    });
  });

  // Pencil ✎ — open/close a block's options menu (colour + notes / clock).
  // Clicking the block itself no longer opens anything; only the pencil does.
  window.toggleMenu = function (e, btn) {
    e.preventDefault();
    e.stopPropagation();
    var block = btn.closest(".cblock");
    var open = block.classList.contains("menu-open");
    document.querySelectorAll(".cblock.menu-open").forEach(function (b) { b.classList.remove("menu-open"); });
    if (!open) block.classList.add("menu-open");
  };
  // A press anywhere else (including starting a drag) closes an open menu.
  document.addEventListener("pointerdown", function (e) {
    if (e.target.closest(".cb-edit") || e.target.closest(".cb-menu")) return;
    document.querySelectorAll(".cblock.menu-open").forEach(function (b) { b.classList.remove("menu-open"); });
  });

  // Delete a shift (the ✕ on the block corner).
  window.deleteShift = function (e, id) {
    e.preventDefault();
    e.stopPropagation();
    if (window.confirm("Delete this shift?")) post("/schedule/shifts/" + id + "/delete", {});
  };

  // One-click position/colour picker — preserves the shift's person and times.
  window.setPosition = function (e, id, positionId) {
    e.preventDefault();
    e.stopPropagation();
    var b = document.querySelector('.cblock[data-shift-id="' + id + '"]');
    if (!b) return;
    var col = b.closest(".cal-col");
    var startMin = parseInt(b.dataset.startMin, 10);
    var dur = parseInt(b.dataset.duration || String(DEFAULT_MIN), 10);
    post("/schedule/shifts/" + id + "/edit", {
      staff_id: b.dataset.staffId || "0", position_id: positionId,
      date: col.dataset.date, start: hhmm(startMin), end: hhmm(startMin + dur),
      notes: b.dataset.notes || "",
    });
  };
})();
