// After deploying the API to Render, replace this with its URL, e.g.
// "https://athletics-api.onrender.com"
const API_BASE_URL = "http://127.0.0.1:8123";

async function api(path) {
  const res = await fetch(`${API_BASE_URL}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function fillSelect(select, values, placeholder) {
  select.innerHTML = "";
  if (placeholder) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = placeholder;
    select.appendChild(opt);
  }
  for (const v of values) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    select.appendChild(opt);
  }
}

async function loadSeasons() {
  const seasons = await api("/seasons");
  fillSelect(document.getElementById("athlete-year-select"), seasons, "All years");
  fillSelect(document.getElementById("club-year-select"), seasons, "All-time");
}

// ---------- Athlete panel ----------

let currentAthleteId = null;
let currentAthleteResults = [];
let chart = null;

async function searchAthletes() {
  const name = document.getElementById("athlete-name").value.trim();
  const list = document.getElementById("athlete-matches");
  list.innerHTML = "";
  if (!name) return;

  const matches = await api(`/athletes/search?name=${encodeURIComponent(name)}`);
  for (const m of matches) {
    const li = document.createElement("li");
    li.textContent = `${m.name} (b. ${m.birth_year ?? "?"})`;
    li.addEventListener("click", () => selectAthlete(m.id, m.name));
    list.appendChild(li);
  }
  if (matches.length === 0) {
    list.innerHTML = "<li class='empty'>No matches</li>";
  }
}

async function selectAthlete(id, name) {
  currentAthleteId = id;
  document.getElementById("athlete-matches").innerHTML = "";
  document.getElementById("athlete-name").value = name;
  document.getElementById("athlete-detail").classList.remove("hidden");
  document.getElementById("athlete-detail-name").textContent = name;
  document.getElementById("athlete-year-select").value = "";
  await loadAthleteCareer();
}

async function loadAthleteCareer() {
  const year = document.getElementById("athlete-year-select").value;
  const data = year
    ? await api(`/athletes/${currentAthleteId}/${year}`)
    : await api(`/athletes/${currentAthleteId}/career`);
  currentAthleteResults = data.results;

  const disciplines = [...new Set(currentAthleteResults.map(r => r.discipline))].sort();
  const disciplineSelect = document.getElementById("discipline-select");
  fillSelect(disciplineSelect, disciplines, null);

  renderAthleteTable(currentAthleteResults);
  if (disciplines.length) renderProgressionChart(disciplines[0]);
}

function renderAthleteTable(results) {
  const tbody = document.querySelector("#athlete-results-table tbody");
  tbody.innerHTML = "";
  for (const r of results) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${r.year}</td><td>${r.discipline}</td><td>${r.mark}</td>
      <td>${r.date ?? ""}</td><td>${r.city ?? ""}</td><td>${r.agegroup}</td>
      <td>${r.rank ?? ""}</td><td>${r.club ?? ""}</td><td>${r.wa_points ?? ""}</td>`;
    tbody.appendChild(tr);
  }
}

function renderProgressionChart(discipline) {
  const points = currentAthleteResults
    .filter(r => r.discipline === discipline && r.mark_value != null)
    .sort((a, b) => (a.date ?? "").localeCompare(b.date ?? ""));

  const ctx = document.getElementById("progression-chart");
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: points.map(p => p.date),
      datasets: [{
        label: discipline,
        data: points.map(p => p.mark_value),
        borderColor: "#2563eb",
        tension: 0.2,
      }],
    },
    options: { responsive: true, scales: { y: { title: { display: true, text: "mark value" } } } },
  });
}

// ---------- Club panel ----------

async function searchClub() {
  const code = document.getElementById("club-code").value.trim().toUpperCase();
  const year = document.getElementById("club-year-select").value;
  if (!code) return;

  const detail = document.getElementById("club-detail");
  try {
    const [stats, records] = await Promise.all([
      year ? api(`/clubs/${code}/statistics?year=${year}`) : Promise.resolve(null),
      api(`/clubs/${code}/records${year ? `?year=${year}` : ""}`),
    ]);
    detail.classList.remove("hidden");
    renderClubStats(stats);
    renderClubRecords(records.records);
  } catch (e) {
    detail.classList.remove("hidden");
    document.getElementById("club-stats").innerHTML = `<p class="empty">${e.message}</p>`;
    document.querySelector("#club-records-table tbody").innerHTML = "";
  }
}

function renderClubStats(stats) {
  const el = document.getElementById("club-stats");
  if (!stats) {
    el.innerHTML = "<p class='hint'>Pick a year to see season statistics.</p>";
    return;
  }
  el.innerHTML = `
    <div><strong>${stats.athletes}</strong><span>athletes</span></div>
    <div><strong>${stats.races_entered}</strong><span>races entered</span></div>
    <div><strong>${stats.podium_finishes}</strong><span>podiums</span></div>
    <div><strong>${stats.wa_points_avg ? stats.wa_points_avg.toFixed(0) : "-"}</strong><span>avg WA points</span></div>`;
}

function renderClubRecords(records) {
  const tbody = document.querySelector("#club-records-table tbody");
  tbody.innerHTML = "";
  for (const r of records) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${r.discipline}${r.female ? " (W)" : " (M)"}${r.indoor ? " indoor" : ""}</td>
      <td>${r.mark}</td><td>${r.athlete_name}</td><td>${r.date ?? ""}</td><td>${r.city ?? ""}</td>`;
    tbody.appendChild(tr);
  }
}

// ---------- wiring ----------

document.getElementById("athlete-search-btn").addEventListener("click", searchAthletes);
document.getElementById("athlete-name").addEventListener("keydown", e => { if (e.key === "Enter") searchAthletes(); });
document.getElementById("athlete-year-select").addEventListener("change", loadAthleteCareer);
document.getElementById("discipline-select").addEventListener("change", e => renderProgressionChart(e.target.value));
document.getElementById("club-search-btn").addEventListener("click", searchClub);
document.getElementById("club-code").addEventListener("keydown", e => { if (e.key === "Enter") searchClub(); });

loadSeasons();
