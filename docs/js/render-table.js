// Shared DataTable wiring used by all export pages -- assumes a <table
// id="DataTable"> already exists in #DataTableDiv, hands it to DataTables
// for sorting + Excel export. `order` is DataTables' initial sort (default
// [[0, 'asc']] -- its own out-of-the-box behavior, made explicit here so a
// caller that already built rows in a deliberate order, like
// athleteCareer.html's "most years first," can pass order=[] to keep that
// order instead of DataTables silently re-sorting column 0 ascending on
// load. Clicking a header still sorts interactively either way -- this only
// changes what's shown before the user clicks anything.
function initDataTable(order = [[0, 'asc']]) {
  const table = $("#DataTable").DataTable({
    paging: false,
    ordering: true,
    info: false,
    searching: false,
    order,
  });
  new $.fn.DataTable.Buttons(table, {
    buttons: [{
      extend: "excel",
      text: "Export Data",
      className: "btn btn-outline-primary btn-light",
    }],
  }).container().appendTo($("#buttons"));
  $(".buttons-excel").removeClass("btn-secondary");
}

// Builds a plain Bootstrap table from an array of row objects + a column
// spec, then wires it up via initDataTable(). Used for flat (one row per
// record) tables; see athleteCareer.html for the pivoted variant.
function renderDataTable(rows, columns) {
  $("#DataTableDiv").empty();
  $(".buttons-excel").remove();

  const thead = columns.map(c => `<th class="text-center">${c.label}</th>`).join("");
  const tbody = rows.map(row => {
    const cells = columns.map(c => {
      const value = c.format ? c.format(row) : (row[c.field] ?? "");
      return `<td class="text-center">${value}</td>`;
    }).join("");
    return `<tr>${cells}</tr>`;
  }).join("");

  $("#DataTableDiv").append(
    `<table id="DataTable" class="table table-bordered table-striped border-dark table-primary">
       <thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody>
     </table>`
  );

  initDataTable();
}
