/* Schedule drag-and-drop.
 *
 * Managers schedule by dragging: drop a teammate from the Team panel onto a day
 * to create a shift, drag a shift block to move it, and drag a block's bottom
 * edge to resize. Every gesture just posts to the existing create/edit routes
 * and reloads — no client-side state to drift out of sync. Non-managers have no
 * draggable elements, so this is a no-op for them.
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
  var startHour = parseInt(body.dataset.startHour || "8", 10);

  function minuteFromY(col, clientY) {
    var rect = col.getBoundingClientRect();
    var min = startHour * 60 + ((clientY - rect.top) / PPH) * 60;
    min = Math.round(min / SNAP) * SNAP;
    return Math.max(startHour * 60, min);
  }

  function hhmm(min) {
    min = ((Math.round(min) % 1440) + 1440) % 1440;   // wrap past midnight
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

  // ---- Drag sources -------------------------------------------------------
  document.querySelectorAll('.team-row[draggable="true"]').forEach(function (row) {
    row.addEventListener("dragstart", function (e) {
      e.dataTransfer.setData("text/plain", JSON.stringify({ t: "new", staff: row.dataset.staffId }));
    });
  });

  document.querySelectorAll('.cblock[draggable="true"]').forEach(function (b) {
    b.addEventListener("dragstart", function (e) {
      e.stopPropagation();
      e.dataTransfer.setData("text/plain", JSON.stringify({
        t: "move",
        id: b.dataset.shiftId,
        staff: b.dataset.staffId || "0",
        pos: b.dataset.positionId || "0",
        notes: b.dataset.notes || "",
        dur: b.dataset.duration || String(DEFAULT_MIN),
      }));
    });
  });

  // ---- Drop targets: the day columns -------------------------------------
  document.querySelectorAll(".cal-col[data-date]").forEach(function (col) {
    col.addEventListener("dragover", function (e) {
      e.preventDefault();
      col.classList.add("cal-col-over");
    });
    col.addEventListener("dragleave", function () { col.classList.remove("cal-col-over"); });
    col.addEventListener("drop", function (e) {
      e.preventDefault();
      col.classList.remove("cal-col-over");
      var raw = e.dataTransfer.getData("text/plain");
      if (!raw) return;
      var d = JSON.parse(raw);
      var start = minuteFromY(col, e.clientY);
      if (d.t === "new") {
        post("/schedule/shifts", {
          staff_id: d.staff, position_id: 0, date: col.dataset.date,
          start: hhmm(start), end: hhmm(start + DEFAULT_MIN), notes: "",
        });
      } else if (d.t === "move") {
        var dur = parseInt(d.dur || String(DEFAULT_MIN), 10);
        post("/schedule/shifts/" + d.id + "/edit", {
          staff_id: d.staff, position_id: d.pos, date: col.dataset.date,
          start: hhmm(start), end: hhmm(start + dur), notes: d.notes,
        });
      }
    });
  });

  // ---- Resize by dragging a block's bottom edge --------------------------
  // The block is natively draggable (for moving), which would otherwise hijack
  // an edge drag — so we turn draggable off for the duration of the resize and
  // preview the new height live, then post the new end time on release.
  document.querySelectorAll(".cblock .cb-resize").forEach(function (handle) {
    handle.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var b = handle.closest(".cblock");
      var col = b.closest(".cal-col");
      var startMin = parseInt(b.dataset.startMin, 10);
      var endMin = startMin + parseInt(b.dataset.duration || "60", 10);

      b.draggable = false;
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
        b.draggable = true;
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
    if (window.confirm("Delete this shift?")) {
      post("/schedule/shifts/" + id + "/delete", {});
    }
  };
})();
