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
  function post(url, data) {
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(data).toString(),
    }).then(function () { location.reload(); })
      .catch(function () { location.reload(); });
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
    var offY = 0, w = 150, h = 40;
    if (type === "move") {
      var r = source.getBoundingClientRect();
      offY = e.clientY - r.top; w = r.width; h = r.height;
      ghost.style.setProperty("--pc", getComputedStyle(source).getPropertyValue("--pc") || "#3b82f6");
      var nm = source.querySelector(".cb-name"), tm = source.querySelector(".cb-time");
      ghost.innerHTML = "<b>" + (tm ? tm.textContent : "") + "</b><br>" + (nm ? nm.textContent : "");
      ghost.style.height = h + "px";
    } else {
      ghost.textContent = data.name || "New shift";
    }
    ghost.style.width = w + "px";
    drag = { type: type, source: source, data: data, offY: offY,
             startX: e.clientX, startY: e.clientY, moved: false, ghost: ghost };
    document.addEventListener("pointermove", onDragMove);
    document.addEventListener("pointerup", onDragUp);
  }

  function onDragMove(e) {
    if (!drag) return;
    if (!drag.moved) {
      if (Math.abs(e.clientX - drag.startX) < THRESH && Math.abs(e.clientY - drag.startY) < THRESH) return;
      drag.moved = true;
      document.body.appendChild(drag.ghost);
      if (drag.type === "move") drag.source.style.visibility = "hidden";
    }
    drag.ghost.style.left = (e.clientX + 8) + "px";
    drag.ghost.style.top = (e.clientY - drag.offY) + "px";
    clearOver();
    var col = columnAt(e.clientX, e.clientY);
    if (col) col.classList.add("cal-col-over");
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
    var start = minuteFromY(col, e.clientY - d.offY);
    if (d.type === "new") {
      post("/schedule/shifts", {
        staff_id: d.data.staff, position_id: 0, date: col.dataset.date,
        start: hhmm(start), end: hhmm(start + DEFAULT_MIN), notes: "",
      });
    } else {
      var b = d.source, dur = parseInt(b.dataset.duration || String(DEFAULT_MIN), 10);
      post("/schedule/shifts/" + b.dataset.shiftId + "/edit", {
        staff_id: b.dataset.staffId || "0", position_id: b.dataset.positionId || "0",
        date: col.dataset.date, start: hhmm(start), end: hhmm(start + dur),
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
