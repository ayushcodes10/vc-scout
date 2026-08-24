/* VC Scout - portfolio filtering, sorting and search.
 *
 * Every row is server-rendered. This file only decides which of them are visible and in
 * what order: it toggles the `hidden` attribute and reorders existing nodes. It never
 * builds markup from data, and the one string it writes - the result count - goes through
 * textContent. No markup-assigning property is used anywhere here, and there is no network
 * access.
 *
 * The filter records come from a <script type="application/json"> block whose contents
 * were escaped when the page was generated, so no value in it can close that element.
 */
(function () {
  "use strict";

  var dataNode = document.getElementById("vcs-filter-data");
  var table = document.getElementById("vcs-table");
  if (!dataNode || !table) return;

  var records;
  try {
    records = JSON.parse(dataNode.textContent || "[]");
  } catch (error) {
    return; // Leave the server-rendered table exactly as it is.
  }

  var body = table.tBodies[0];
  var rows = {};
  Array.prototype.forEach.call(body.rows, function (row) {
    rows[row.getAttribute("data-company")] = row;
  });

  var search = document.getElementById("vcs-search");
  var call = document.getElementById("vcs-call");
  var confidence = document.getElementById("vcs-confidence");
  var fit = document.getElementById("vcs-fit");
  var sort = document.getElementById("vcs-sort");
  var reset = document.getElementById("vcs-reset");
  var count = document.getElementById("vcs-count");
  var empty = document.getElementById("vcs-empty");

  var CALL_ORDER = { "take-a-meeting": 0, watch: 1, pass: 2 };
  var CONFIDENCE_ORDER = { high: 0, medium: 1, low: 2 };

  function value(node) {
    return node && node.value ? node.value : "";
  }

  function matches(record, term) {
    if (term && record.text.indexOf(term) === -1) return false;
    if (value(call) && record.call !== value(call)) return false;
    if (value(confidence) && record.confidence !== value(confidence)) return false;
    if (value(fit) && record.fit !== value(fit)) return false;
    return true;
  }

  function comparator(mode) {
    if (mode === "score") {
      return function (a, b) {
        return b.score - a.score || a.rank - b.rank;
      };
    }
    if (mode === "confidence") {
      return function (a, b) {
        return (
          CONFIDENCE_ORDER[a.confidence] - CONFIDENCE_ORDER[b.confidence] || a.rank - b.rank
        );
      };
    }
    if (mode === "name") {
      return function (a, b) {
        return a.name < b.name ? -1 : a.name > b.name ? 1 : a.rank - b.rank;
      };
    }
    return function (a, b) {
      return (
        CALL_ORDER[a.call] - CALL_ORDER[b.call] || a.rank - b.rank
      );
    };
  }

  function apply() {
    var term = value(search).trim().toLowerCase();
    var visible = 0;

    records.forEach(function (record) {
      var row = rows[record.id];
      if (!row) return;
      var show = matches(record, term);
      row.hidden = !show;
      if (show) visible += 1;
    });

    var ordered = records.slice().sort(comparator(value(sort)));
    ordered.forEach(function (record) {
      var row = rows[record.id];
      if (row) body.appendChild(row);
    });

    if (count) {
      count.textContent =
        visible === records.length
          ? "Showing all " + records.length + " analysed candidates."
          : "Showing " + visible + " of " + records.length + " analysed candidates.";
    }
    if (empty) empty.hidden = visible !== 0;
  }

  [search, call, confidence, fit, sort].forEach(function (node) {
    if (!node) return;
    node.addEventListener("input", apply);
    node.addEventListener("change", apply);
  });

  if (reset) {
    reset.addEventListener("click", function () {
      if (search) search.value = "";
      [call, confidence, fit].forEach(function (node) {
        if (node) node.value = "";
      });
      if (sort) sort.value = "workflow";
      apply();
      if (search) search.focus();
    });
  }

  var controls = document.getElementById("vcs-controls");
  if (controls) {
    controls.hidden = false;
    controls.addEventListener("submit", function (event) {
      event.preventDefault(); // Filtering is live; there is nothing to submit.
    });
  }

  apply();
})();
