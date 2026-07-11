// Wires a name-search input in either "dropdown" (live autocomplete) or
// "classic" (type + Search button + click a match from a list) mode, based
// on the site-wide preference set on the home page (see data-cache.js, whose
// globals -- getSearchMode/removeDiacritics -- this relies on being loaded
// first as a plain script). Both modes filter the same pre-fetched, cached
// item list client-side -- classic mode isn't a network fallback, just a
// different interaction style.
//
// items: array of {label, ...} objects (already fetched/cached by the caller)
// valueField: which field on each item is the id/code to hand back on selection
// onSelect(item): called with the selected item (original fields plus .value)
import Autocomplete from "./autocomplete.js";

export function setupSearch({ inputId, buttonId, matchListId, items, valueField, onSelect }) {
  const input = document.getElementById(inputId);
  const button = document.getElementById(buttonId);
  const matchList = document.getElementById(matchListId);

  if (getSearchMode() === "classic") {
    button.style.display = "";
    matchList.style.display = "";

    function runSearch() {
      const q = removeDiacritics(input.value).toLowerCase();
      matchList.innerHTML = "";
      if (!q) return;
      const matches = items
        .filter(item => removeDiacritics(item.label).toLowerCase().includes(q))
        .slice(0, 25);
      for (const item of matches) {
        const li = document.createElement("li");
        li.className = "list-group-item list-group-item-action";
        li.textContent = item.label;
        li.addEventListener("click", () => {
          input.value = item.label;
          matchList.innerHTML = "";
          onSelect({ ...item, value: item[valueField] });
        });
        matchList.appendChild(li);
      }
      if (matches.length === 0) {
        matchList.innerHTML = "<li class='list-group-item text-muted'>No matches</li>";
      }
    }
    button.addEventListener("click", runSearch);
    input.addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });
    return;
  }

  // Dropdown mode: Autocomplete.js filters `items` client-side as you type
  // (see its `items` config path), no network call per keystroke.
  new Autocomplete(input, {
    items,
    labelField: "label",
    valueField,
    maximumItems: 8,
    onSelectItem: onSelect,
  });
}
