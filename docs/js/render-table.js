// Shared DataTable renderer used by all four export pages. Builds a plain
// Bootstrap table from an array of row objects + a column spec, then hands
// it to DataTables for sorting + Excel export -- avoids re-deriving this
// per page.
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

  const table = $("#DataTable").DataTable({
    paging: false,
    ordering: true,
    info: false,
    searching: false,
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
