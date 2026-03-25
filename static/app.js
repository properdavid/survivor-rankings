/**
 * Survivor Rankings App — Multi-Season
 *
 * Single-page application that lets users predict the elimination order of
 * Survivor contestants. Users sign in via Google OAuth, drag-and-drop
 * contestants into a ranked list (1 = predicted winner, N = predicted first out),
 * and earn points as the season progresses based on how close their predictions
 * match the actual outcomes.
 *
 * Architecture:
 *   - FastAPI backend serves the API at /api/* and auth at /auth/*
 *   - This file drives the entire client: auth, ranking, scoring, leaderboard, admin
 *   - State is kept in module-level variables; the DOM is re-rendered on change
 *   - Rankings lock once the admin records the first elimination/removal
 *   - All data endpoints accept ?season=<id> to scope by season
 */

// --- State ---
let currentUser = null;
let currentSeason = null;  // { id, number, name, is_active }
let seasons = [];
let contestants = [];
let tribes = [];
let myRankings = [];
let draggedItem = null;
let discussionThreads = [];
let currentThread = null;
let currentThreadPosts = [];
let discussionPage = 1;
let discussionTotalPages = 1;

// --- Season helper ---
function seasonParam(prefix = "?") {
    return currentSeason ? `${prefix}season=${currentSeason.id}` : "";
}

// --- Service Worker (PWA) ---
if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}

// --- Init ---
document.addEventListener("DOMContentLoaded", async () => {
    await loadSeasons();
    await checkAuth();
    await loadContestants();
    await loadTribes();
    setupTabs();
    setupDragAndDrop();
    updateSeasonUI();

    if (currentUser) {
        await loadMyRankings();
    }
});

// --- Compact Header (mobile) ---
(function () {
    const header = document.querySelector("header");
    const COMPACT_THRESHOLD = 50;
    let ticking = false;
    let isCompact = false;

    window.addEventListener("scroll", () => {
        if (!ticking) {
            ticking = true;
            requestAnimationFrame(() => {
                if (window.innerWidth <= 768) {
                    if (!isCompact && window.scrollY > COMPACT_THRESHOLD) {
                        // Enter compact — check that the page is actually tall enough
                        // to stay scrolled after the header shrinks (prevents oscillation).
                        const viewportH = window.innerHeight;
                        const headerH = header.offsetHeight;
                        const docH = document.documentElement.scrollHeight;
                        const headroom = docH - headerH - viewportH;
                        if (headroom > COMPACT_THRESHOLD) {
                            header.classList.add("compact");
                            isCompact = true;
                        }
                    } else if (isCompact && window.scrollY < 10) {
                        header.classList.remove("compact");
                        isCompact = false;
                    }
                } else {
                    header.classList.remove("compact");
                    isCompact = false;
                }
                ticking = false;
            });
        }
    }, { passive: true });
})();

// --- Pull to Refresh (PWA) ---
// In standalone PWA mode there's no browser pull-to-refresh. This implements
// a touch-based gesture: pull down from the top of the page to reload data.
(function () {
    const ptr = document.getElementById("pull-to-refresh");
    if (!ptr) return;
    const THRESHOLD = 80;
    const MAX_PULL = 120;
    let startY = 0;
    let pulling = false;

    document.addEventListener("touchstart", (e) => {
        if (window.scrollY > 0 || touchItem || ptr.classList.contains("refreshing")) return;
        startY = e.touches[0].clientY;
        pulling = true;
    }, { passive: true });

    document.addEventListener("touchmove", (e) => {
        if (!pulling) return;
        const dy = Math.min(e.touches[0].clientY - startY, MAX_PULL);
        if (dy <= 0) { ptr.style.height = "0"; ptr.classList.remove("pulling"); return; }
        e.preventDefault();
        ptr.classList.add("pulling");
        ptr.style.height = `${dy * 0.5}px`;
        ptr.querySelector(".ptr-spinner").style.transform = `rotate(${dy * 3}deg)`;
    }, { passive: false });

    document.addEventListener("touchend", async () => {
        if (!pulling) return;
        pulling = false;
        const height = parseFloat(ptr.style.height) || 0;

        if (height >= THRESHOLD * 0.5) {
            ptr.classList.remove("pulling");
            ptr.classList.add("refreshing");
            ptr.style.height = "";
            ptr.querySelector(".ptr-spinner").style.transform = "";

            await loadContestants();
            await loadTribes();
            const activeTab = document.querySelector(".nav-btn.active")?.dataset.tab;
            if (activeTab === "rankings" && currentUser) await loadMyRankings();
            if (activeTab === "scores" && currentUser) await loadMyScores();
            if (activeTab === "leaderboard") await loadLeaderboard();
            if (activeTab === "discussion") await loadDiscussionTab();
            if (activeTab === "admin" && currentUser?.is_admin) renderAdminPanel();

            ptr.classList.remove("refreshing");
        } else {
            ptr.classList.remove("pulling");
            ptr.style.height = "0";
            ptr.querySelector(".ptr-spinner").style.transform = "";
        }
    });
})();

// --- Seasons ---

async function loadSeasons() {
    try {
        const res = await fetch("/api/seasons");
        if (!res.ok) return;
        seasons = await res.json();
        currentSeason = seasons.find(s => s.is_active) || seasons[0] || null;
        renderSeasonSelect();
    } catch (e) {
        console.error("Failed to load seasons:", e);
    }
}

function renderSeasonSelect() {
    const select = document.getElementById("season-select");
    if (!select || !seasons.length) return;
    select.innerHTML = seasons.map(s =>
        `<option value="${s.id}" ${s.id === currentSeason?.id ? "selected" : ""}>${escapeHtml(s.name)}</option>`
    ).join("");
}

async function switchSeason(seasonId) {
    currentSeason = seasons.find(s => s.id == seasonId) || currentSeason;
    await loadContestants();
    await loadTribes();
    updateSeasonUI();
    if (currentUser) await loadMyRankings();
    const activeTab = document.querySelector(".nav-btn.active")?.dataset.tab;
    if (activeTab === "scores" && currentUser) loadMyScores();
    if (activeTab === "leaderboard") loadLeaderboard();
    if (activeTab === "discussion") { currentThread = null; loadDiscussionTab(); }
    if (activeTab === "admin" && currentUser?.is_admin) renderAdminPanel();
}

function updateSeasonUI() {
    const numEl = document.getElementById("season-number");
    if (numEl && currentSeason) numEl.textContent = currentSeason.number;

    document.title = currentSeason ? `Survivor ${currentSeason.number} Rankings` : "Survivor Rankings";

    const rankLabel = document.getElementById("rank-last-label");
    if (rankLabel) rankLabel.textContent = `${contestants.length} = Predicted First Out`;

    const banner = document.getElementById("past-season-banner");
    const saveArea = document.getElementById("save-area");
    if (currentSeason && !currentSeason.is_active) {
        if (banner) banner.style.display = "block";
        if (saveArea) saveArea.style.display = "none";
    } else {
        if (banner) banner.style.display = "none";
        if (saveArea && currentUser) saveArea.style.display = "flex";
    }
}

// --- Tribe Styles ---

function hexToRgba(hex, alpha) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function injectTribeStyles(tribeList) {
    let styleEl = document.getElementById("tribe-dynamic-styles");
    if (!styleEl) {
        styleEl = document.createElement("style");
        styleEl.id = "tribe-dynamic-styles";
        document.head.appendChild(styleEl);
    }
    styleEl.textContent = tribeList.map(t => {
        const slug = t.name.toLowerCase();
        const bg = hexToRgba(t.color, 0.2);
        const border = hexToRgba(t.color, 0.4);
        return [
            `.tribe-${slug} { background: ${bg} !important; color: ${t.color} !important; border: 1px solid ${border} !important; }`,
            `.tribe-select.tribe-${slug} { background-color: ${bg} !important; color: ${t.color} !important; border: 1px solid ${border} !important; }`,
        ].join("\n");
    }).join("\n");
}

async function loadTribes() {
    try {
        const res = await fetch(`/api/tribes${seasonParam()}`);
        if (!res.ok) return;
        tribes = await res.json();
        injectTribeStyles(tribes);
    } catch (e) {
        console.error("Failed to load tribes:", e);
    }
}

// --- Auth ---

async function checkAuth() {
    try {
        const res = await fetch("/auth/me");
        const data = await res.json();

        if (data.authenticated) {
            currentUser = data;
            renderUserArea();
        } else {
            currentUser = null;
            document.getElementById("not-logged-in-msg").style.display = "block";
        }
    } catch (e) {
        console.error("Auth check failed:", e);
    }
}

function renderUserArea() {
    const area = document.getElementById("user-area");
    area.innerHTML = `
        <div class="user-info">
            ${currentUser.picture ? `<img src="${currentUser.picture}" alt="" class="user-avatar" referrerpolicy="no-referrer">` : ""}
            <span class="user-name">${escapeHtml(currentUser.name)}</span>
        </div>
        <a href="/auth/logout" class="logout-btn">Sign out</a>
    `;

    document.getElementById("not-logged-in-msg").style.display = "none";
    if (currentSeason?.is_active) {
        document.getElementById("save-area").style.display = "flex";
    }

    document.querySelectorAll(".auth-only").forEach(el => el.style.display = "inline-block");
    if (currentUser.is_admin) {
        document.querySelectorAll(".admin-only").forEach(el => el.style.display = "inline-block");
    }
}

// --- Tabs ---

function setupTabs() {
    document.querySelectorAll(".nav-btn").forEach(btn => {
        btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });
}

function switchTab(tab) {
    document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));

    document.querySelector(`[data-tab="${tab}"]`).classList.add("active");
    document.getElementById(`tab-${tab}`).classList.add("active");

    if (tab === "scores" && currentUser) loadMyScores();
    if (tab === "leaderboard") loadLeaderboard();
    if (tab === "discussion") loadDiscussionTab();
    if (tab === "admin" && currentUser?.is_admin) renderAdminPanel();
}

// --- Contestants ---
async function loadContestants() {
    try {
        const res = await fetch(`/api/contestants${seasonParam()}`);
        contestants = await res.json();
    } catch (e) {
        console.error("Failed to load contestants:", e);
    }
}

// --- Rankings ---

async function loadMyRankings() {
    if (!currentUser) return;

    try {
        const res = await fetch(`/api/rankings${seasonParam()}`);
        if (res.ok) {
            myRankings = await res.json();
        }
    } catch (e) {
        console.error("Failed to load rankings:", e);
    }

    renderRankingList();
}

function renderRankingList() {
    const list = document.getElementById("ranking-list");
    const lockedMsg = document.getElementById("rankings-locked-msg");

    let items;
    let isLocked = false;
    const isPastSeason = currentSeason && !currentSeason.is_active;

    if (myRankings.length > 0) {
        items = myRankings.map(r => ({
            id: r.contestant_id,
            name: r.contestant_name,
            tribe: r.tribe,
            image_url: r.image_url,
            rank: r.rank,
            locked: r.locked,
            scoring_eligible: r.scoring_eligible,
            elimination_order: r.elimination_order,
            is_winner: r.is_winner,
        }));
        isLocked = items.some(i => i.locked) || isPastSeason;
    } else {
        items = contestants.map((c, i) => ({
            id: c.id,
            name: c.name,
            tribe: c.tribe,
            image_url: c.image_url,
            rank: i + 1,
            locked: false,
            elimination_order: c.elimination_order,
            is_winner: c.is_winner,
        }));
        if (isPastSeason) isLocked = true;
    }

    if (isLocked) {
        lockedMsg.style.display = isPastSeason ? "none" : "block";
        document.getElementById("save-area").style.display = "none";
    } else {
        lockedMsg.style.display = "none";
    }

    list.innerHTML = items.map(item => `
        <div class="ranking-item ${isLocked ? 'locked' : ''} rank-${item.rank}"
             data-contestant-id="${item.id}"
             data-rank="${item.rank}">
            <span class="drag-handle">${isLocked ? '🔒' : '⠿'}</span>
            <span class="rank-number">#${item.rank}</span>
            ${item.image_url ? `<img src="/api/image-proxy?url=${encodeURIComponent(item.image_url)}" alt="${escapeHtml(item.name)}" class="contestant-photo">` : ''}
            <span class="contestant-name">${escapeHtml(item.name)}</span>
            <span class="tribe-badge tribe-${(item.tribe || '').toLowerCase()}">${escapeHtml(item.tribe || '')}</span>
            ${item.is_winner ? '<span class="winner-badge">👑 Winner</span>' : ''}
            ${item.elimination_order && !item.is_winner ? `<span class="eliminated-badge">Out #${item.elimination_order}</span>` : ''}
            ${item.scoring_eligible === false ? '<span class="late-badge">Late pick</span>' : ''}
        </div>
    `).join("");

    setupDragAndDrop();
}

// --- Drag & Drop ---

function setupDragAndDrop() {
    const items = document.querySelectorAll(".ranking-item:not(.locked)");

    items.forEach(item => {
        const handle = item.querySelector(".drag-handle");

        if (handle) {
            handle.addEventListener("mousedown", () => {
                item.draggable = true;
                document.addEventListener("mouseup", () => { item.draggable = false; }, { once: true });
            });
        }

        item.addEventListener("dragstart", handleDragStart);
        item.addEventListener("dragend", handleDragEnd);
        item.addEventListener("dragover", handleDragOver);
        item.addEventListener("dragenter", handleDragEnter);
        item.addEventListener("dragleave", handleDragLeave);
        item.addEventListener("drop", handleDrop);

        if (handle) {
            handle.addEventListener("touchstart", (e) => {
                touchItem = item;
                touchStartY = e.touches[0].clientY;
                item.classList.add("dragging");
                e.preventDefault();
            }, { passive: false });
        }
    });
}

function handleDragStart(e) {
    draggedItem = this;
    this.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", this.dataset.rank);
}

function handleDragEnd(_e) {
    this.classList.remove("dragging");
    this.draggable = false;
    document.querySelectorAll(".ranking-item").forEach(item => {
        item.classList.remove("drag-over");
    });
    draggedItem = null;
}

function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
}

function handleDragEnter(e) {
    e.preventDefault();
    this.classList.add("drag-over");
}

function handleDragLeave(_e) {
    this.classList.remove("drag-over");
}

function handleDrop(e) {
    e.preventDefault();
    this.classList.remove("drag-over");

    if (draggedItem === this) return;

    const list = document.getElementById("ranking-list");
    const allItems = [...list.querySelectorAll(".ranking-item")];
    const fromIndex = allItems.indexOf(draggedItem);
    const toIndex = allItems.indexOf(this);

    if (fromIndex < toIndex) {
        this.parentNode.insertBefore(draggedItem, this.nextSibling);
    } else {
        this.parentNode.insertBefore(draggedItem, this);
    }

    updateRankNumbers();
}

let touchStartY = 0;
let touchItem = null;

document.addEventListener("touchmove", function (e) {
    if (!touchItem) return;
    e.preventDefault();

    const touch = e.touches[0];
    const elementBelow = document.elementFromPoint(touch.clientX, touch.clientY);

    document.querySelectorAll(".ranking-item").forEach(item => item.classList.remove("drag-over"));

    if (elementBelow) {
        const targetItem = elementBelow.closest(".ranking-item");
        if (targetItem && targetItem !== touchItem) {
            targetItem.classList.add("drag-over");
        }
    }
}, { passive: false });

document.addEventListener("touchend", function (_e) {
    if (!touchItem) return;
    touchItem.classList.remove("dragging");

    const dragOverItem = document.querySelector(".ranking-item.drag-over");
    if (dragOverItem) {
        const list = document.getElementById("ranking-list");
        const allItems = [...list.querySelectorAll(".ranking-item")];
        const fromIndex = allItems.indexOf(touchItem);
        const toIndex = allItems.indexOf(dragOverItem);

        if (fromIndex < toIndex) {
            dragOverItem.parentNode.insertBefore(touchItem, dragOverItem.nextSibling);
        } else {
            dragOverItem.parentNode.insertBefore(touchItem, dragOverItem);
        }

        updateRankNumbers();
    }

    document.querySelectorAll(".ranking-item").forEach(item => item.classList.remove("drag-over"));
    touchItem = null;
});

function updateRankNumbers() {
    const items = document.querySelectorAll("#ranking-list .ranking-item");
    items.forEach((item, index) => {
        const rank = index + 1;
        item.dataset.rank = rank;
        item.querySelector(".rank-number").textContent = `#${rank}`;

        item.className = item.className.replace(/rank-\d+/g, "");
        item.classList.add(`rank-${rank}`);
    });
}

// --- Print Rankings ---
function printRankings() {
    const items = document.querySelectorAll("#ranking-list .ranking-item");
    if (!items.length) return;

    const rows = [...items].map(item => {
        const rank = item.dataset.rank;
        const name = item.querySelector(".contestant-name")?.textContent ?? "";
        const tribe = item.querySelector(".tribe-badge")?.textContent ?? "";
        return `<tr><td>${rank}</td><td>${name}</td><td>${tribe}</td></tr>`;
    }).join("");

    const userName = currentUser?.name ?? "My";
    const seasonName = currentSeason?.name ?? "Survivor";
    const html = `<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>${userName}'s ${seasonName} Rankings</title>
    <style>
        body { font-family: Arial, sans-serif; padding: 2rem; color: #000; }
        h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
        p { color: #555; font-size: 0.9rem; margin-bottom: 1.5rem; }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; border-bottom: 2px solid #000; padding: 0.4rem 0.6rem; font-size: 0.85rem; }
        td { padding: 0.35rem 0.6rem; border-bottom: 1px solid #ddd; font-size: 0.9rem; }
        td:first-child { font-weight: 700; width: 3rem; }
    </style>
</head>
<body>
    <h1>🔥 ${seasonName} — ${userName}'s Rankings</h1>
    <p>#1 = Predicted Winner &nbsp;|&nbsp; #${contestants.length} = Predicted First Out</p>
    <table>
        <thead><tr><th>#</th><th>Contestant</th><th>Tribe</th></tr></thead>
        <tbody>${rows}</tbody>
    </table>
</body>
</html>`;

    const blob = new Blob([html], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const win = window.open(url, "_blank");
    win.addEventListener("load", () => {
        win.print();
        URL.revokeObjectURL(url);
    });
}

async function emailRankings() {
    if (!currentUser) {
        showToast("Please sign in first", "error");
        return;
    }

    const btn = document.getElementById("email-btn");
    btn.disabled = true;
    btn.textContent = "Sending...";

    try {
        const res = await fetch(`/api/rankings/email${seasonParam()}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        });
        const data = await res.json();
        if (res.ok) {
            showToast("Rankings emailed to you!", "success");
        } else {
            showToast(data.detail || "Failed to send email", "error");
        }
    } catch (e) {
        showToast("Network error - try again", "error");
    }

    btn.disabled = false;
    btn.textContent = "📧 Email to Me";
}

// --- Save Rankings ---
document.getElementById("save-btn")?.addEventListener("click", saveRankings);

async function saveRankings() {
    if (!currentUser) {
        showToast("Please sign in first", "error");
        return;
    }

    const items = document.querySelectorAll("#ranking-list .ranking-item");
    const rankings = [...items].map((item, index) => ({
        contestant_id: parseInt(item.dataset.contestantId),
        rank: index + 1,
    }));

    const btn = document.getElementById("save-btn");
    const status = document.getElementById("save-status");
    btn.disabled = true;
    status.textContent = "Saving...";

    try {
        const res = await fetch(`/api/rankings${seasonParam()}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rankings }),
        });

        const data = await res.json();

        if (res.ok) {
            status.textContent = "✓ Saved!";
            status.style.color = "var(--success)";
            if (data.late_submission) {
                showToast(`Rankings saved! ${data.ineligible_count} contestant(s) already eliminated — no points for those picks.`, "success");
            } else {
                showToast("Rankings saved successfully!", "success");
            }
            await loadMyRankings();
        } else {
            status.textContent = "Failed to save";
            status.style.color = "var(--danger)";
            showToast(data.detail || "Failed to save rankings", "error");
        }
    } catch (e) {
        status.textContent = "Error";
        status.style.color = "var(--danger)";
        showToast("Network error - try again", "error");
    }

    btn.disabled = false;
    setTimeout(() => { status.textContent = ""; }, 3000);
}

// --- Scores ---

async function loadMyScores() {
    if (!currentUser) {
        document.getElementById("score-summary").innerHTML = `
            <div class="empty-state">
                <span class="emoji">🔒</span>
                <p>Sign in to see your scores</p>
            </div>
        `;
        return;
    }

    try {
        const res = await fetch(`/api/scores${seasonParam()}`);
        const data = await res.json();
        renderScores(data);
    } catch (e) {
        console.error("Failed to load scores:", e);
    }
}

function renderScores(data) {
    const summary = document.getElementById("score-summary");
    const breakdown = document.getElementById("score-breakdown");

    if (!data.breakdown || data.breakdown.length === 0) {
        summary.innerHTML = `
            <div class="empty-state">
                <span class="emoji">📋</span>
                <p>Submit your rankings first to track your scores!</p>
            </div>
        `;
        breakdown.innerHTML = "";
        return;
    }

    summary.innerHTML = `
        <div class="total-score">${data.total_score}</div>
        <div class="score-label">Total Points</div>
        <div class="score-detail">
            ${data.contestants_scored} of ${data.total_contestants} contestants eliminated
            ${data.max_possible > 0 ? ` • Max possible: ${data.max_possible}` : ""}
        </div>
    `;

    breakdown.innerHTML = data.breakdown.map(b => {
        let pointsClass = "points-pending";
        let pointsText = "—";
        let finalistBadge = "";

        if (b.is_removed) {
            pointsClass = "points-pending";
            pointsText = "—";
            finalistBadge = `<span class="finalist-badge removed-badge">Removed</span>`;
        } else if (b.scoring_ineligible) {
            pointsClass = "points-pending";
            pointsText = "—";
            finalistBadge = `<span class="late-badge">Late prediction</span>`;
        } else if (b.points !== null) {
            if (b.points === b.max_points) pointsClass = "points-perfect";
            else if (b.points > 0) pointsClass = "points-positive";
            else pointsClass = "points-zero";
            pointsText = `${b.points} / ${b.max_points}pts`;
            if (b.finish_position === 1) finalistBadge = `<span class="finalist-badge winner-badge">👑 Winner</span>`;
            else if (b.is_finalist) finalistBadge = `<span class="finalist-badge">Final 3</span>`;
        }

        return `
            <div class="score-row">
                <div class="contestant-info">
                    <span class="rank-number" style="font-size:1.1rem">#${b.user_rank}</span>
                    <div>
                        <span class="contestant-name">${escapeHtml(b.contestant_name)}</span>
                        ${finalistBadge}
                        <div class="prediction-detail">
                            Your rank: #${b.user_rank}
                            ${b.finish_position !== null ? ` → Actual: #${b.finish_position}` : b.is_removed ? " → Removed" : " → Still playing"}${b.scoring_ineligible ? " (no points — already eliminated)" : ""}
                        </div>
                    </div>
                </div>
                <span class="score-points ${pointsClass}">${pointsText}</span>
            </div>
        `;
    }).join("");
}

// --- Leaderboard ---

async function loadLeaderboard() {
    try {
        const res = await fetch(`/api/leaderboard${seasonParam()}`);
        const data = await res.json();
        renderLeaderboard(data);
    } catch (e) {
        console.error("Failed to load leaderboard:", e);
    }
}

function renderLeaderboard(data) {
    const list = document.getElementById("leaderboard-list");

    if (data.length === 0) {
        list.innerHTML = `
            <div class="empty-state">
                <span class="emoji">🏆</span>
                <p>No rankings submitted yet. Be the first!</p>
            </div>
        `;
        return;
    }

    const ranks = [];
    data.forEach((entry, i) => {
        if (i === 0 || entry.total_score < data[i - 1].total_score) {
            ranks.push(i + 1);
        } else {
            ranks.push(ranks[i - 1]);
        }
    });

    list.innerHTML = data.map((entry, i) => `
        <div class="leaderboard-item">
            <span class="leaderboard-rank">${ranks[i]}</span>
            <div class="leaderboard-user">
                ${entry.user_picture ? `<img src="${entry.user_picture}" alt="" class="leaderboard-avatar" referrerpolicy="no-referrer">` : ""}
                <div>
                    <div class="leaderboard-name leaderboard-name-clickable"
                         onclick="viewUserRankings(${entry.user_id}, '${escapeHtml(entry.user_name)}', '/api/users/${entry.user_id}/rankings${seasonParam()}')">
                        ${escapeHtml(entry.user_name)} <span class="leaderboard-name-hint">↗</span>
                    </div>
                    <div class="leaderboard-detail">
                        ${entry.contestants_scored} scored
                        ${entry.max_possible > 0 ? ` • ${Math.round(entry.total_score / entry.max_possible * 100)}% accuracy` : ""}
                    </div>
                </div>
            </div>
            <span class="leaderboard-score">${entry.total_score} pts</span>
        </div>
    `).join("");
}

// --- Admin Sub-Tabs ---

function setupAdminSubTabs() {
    document.querySelectorAll(".admin-sub-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const target = btn.dataset.adminTab;
            document.querySelectorAll(".admin-sub-btn").forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".admin-panel-section").forEach(s => s.style.display = "none");
            btn.classList.add("active");
            document.getElementById(`admin-panel-${target}`).style.display = "block";
        });
    });
}

// --- User Management ---

async function loadUsers() {
    try {
        const res = await fetch(`/api/admin/users${seasonParam()}`);
        if (!res.ok) return;
        const users = await res.json();
        renderUserManagement(users);
    } catch (e) {
        console.error("Failed to load users:", e);
    }
}

function renderUserManagement(users) {
    const list = document.getElementById("admin-users");

    if (users.length === 0) {
        list.innerHTML = `<div class="empty-state"><span class="emoji">👥</span><p>No users registered yet.</p></div>`;
        return;
    }

    list.innerHTML = users.map(u => {
        const isSelf = currentUser && u.id === currentUser.user_id;
        const roleClass = u.is_admin ? "role-admin" : "role-standard";
        const roleLabel = u.is_admin ? "Admin" : "Standard";
        const actionBtn = isSelf ? "" : u.is_admin
            ? `<button class="admin-btn btn-demote" onclick="updateUserRole(${u.id}, false)">Make Standard</button>`
            : `<button class="admin-btn btn-promote" onclick="updateUserRole(${u.id}, true)">Make Admin</button>`;

        const rankingsBadge = u.has_rankings
            ? `<span class="rankings-badge rankings-saved rankings-clickable" onclick="viewUserRankings(${u.id}, '${escapeHtml(u.name)}')">Rankings saved ↗</span>`
            : `<span class="rankings-badge rankings-missing">No rankings</span>`;
        const clearBtn = u.has_rankings
            ? `<button class="admin-btn btn-danger" onclick="deleteUserRankings(${u.id}, '${escapeHtml(u.name)}')">Clear</button>`
            : "";
        const auditBtn = `<button class="admin-btn btn-audit" onclick="viewUserAudit(${u.id})">Audit</button>`;

        return `
            <div class="admin-item user-item">
                ${u.picture ? `<img src="${escapeHtml(u.picture)}" alt="" class="user-avatar-sm" referrerpolicy="no-referrer">` : `<div class="user-avatar-sm user-avatar-placeholder"></div>`}
                <div class="user-details">
                    <span class="contestant-name">${escapeHtml(u.name)}</span>
                    <span class="user-email">${escapeHtml(u.email)}</span>
                </div>
                ${rankingsBadge}
                ${clearBtn}
                ${auditBtn}
                <span class="role-badge ${roleClass}">${roleLabel}</span>
                ${isSelf ? '<span class="self-label">(you)</span>' : ""}
                ${actionBtn}
            </div>
        `;
    }).join("");
}

async function updateUserRole(userId, isAdmin) {
    try {
        const res = await fetch(`/api/admin/users/${userId}/role`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ is_admin: isAdmin }),
        });

        const data = await res.json();
        if (res.ok) {
            showToast(data.message, "success");
            await loadUsers();
        } else {
            showToast(data.detail || "Failed to update role", "error");
        }
    } catch (e) {
        showToast("Network error", "error");
    }
}

async function deleteUserRankings(userId, userName) {
    if (!confirm(`Delete all rankings for ${userName} in ${currentSeason?.name || 'this season'}? This cannot be undone.`)) return;
    try {
        const res = await fetch(`/api/admin/users/${userId}/rankings${seasonParam()}`, { method: "DELETE" });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message, "success");
            await loadUsers();
        } else {
            showToast(data.detail || "Failed to clear rankings", "error");
        }
    } catch (e) {
        showToast("Network error", "error");
    }
}

// --- Admin Panel — Contestant Management ---

function renderContestantList() {
    const list = document.getElementById("admin-contestants");
    if (!list) return;
    list.innerHTML = contestants.map(c => {
        const isEliminated = c.elimination_order !== null;
        const isRemoved = c.is_removed === true;
        const selectOpts = tribes.map(t =>
            `<option value="${escapeHtml(t.name)}" ${c.tribe === t.name ? 'selected' : ''}>${escapeHtml(t.name)}</option>`
        ).join("");
        return `
            <div class="admin-item" data-contestant-id="${c.id}">
                <span class="contestant-name">${escapeHtml(c.name)}</span>
                <select class="tribe-select tribe-${(c.tribe || '').toLowerCase()}"
                        onchange="updateContestantTribe(${c.id}, this)">
                    ${selectOpts}
                </select>
                <div class="admin-controls">
                    ${isEliminated ? `
                        <span class="admin-status ${c.is_winner ? 'status-active' : 'status-eliminated'}">
                            ${c.is_winner ? '👑 Winner' : `Eliminated #${c.elimination_order}`}
                        </span>
                        <button class="admin-btn btn-reset" onclick="resetContestant(${c.id})">Reset</button>
                    ` : isRemoved ? `
                        <span class="admin-status status-removed">${c.elimination_order !== null ? `Removed #${c.elimination_order}` : 'Removed'}</span>
                        <button class="admin-btn btn-reset" onclick="resetContestant(${c.id})">Reset</button>
                    ` : `
                        <span class="admin-status status-active">Active</span>
                        <input type="number" class="admin-input" id="elim-order-${c.id}" min="1" max="${contestants.length}" placeholder="#">
                        <button class="admin-btn btn-eliminate" onclick="eliminateContestant(${c.id})">Eliminate</button>
                        <button class="admin-btn btn-winner" onclick="markWinner(${c.id})">Winner</button>
                        <button class="admin-btn btn-remove" onclick="removeContestant(${c.id})">Remove</button>
                    `}
                </div>
            </div>
        `;
    }).join("");
}

function renderAdminPanel() {
    setupAdminSubTabs();
    loadUsers();
    loadAdminTribes();
    renderContestantList();
    renderSeasonManagement();
    populateAuditUserSelect();
}

// --- Tribe Management (Admin) ---

async function loadAdminTribes() {
    await loadTribes();
    renderTribeManagement(tribes);
}

function renderTribeManagement(tribeList) {
    const container = document.getElementById("admin-tribes-list");
    if (!container) return;

    if (tribeList.length === 0) {
        container.innerHTML = `<div class="empty-state"><span class="emoji">🏝️</span><p>No tribes yet. Add one below.</p></div>`;
        return;
    }

    container.innerHTML = tribeList.map(t => `
        <div class="admin-item tribe-mgmt-item">
            <span class="tribe-badge tribe-${t.name.toLowerCase()}">${escapeHtml(t.name)}</span>
            <div class="tribe-color-control">
                <div class="tribe-color-swatch" style="background:${t.color}; border-color:${t.color}"></div>
                <label class="tribe-color-label">
                    <span>Color</span>
                    <input type="color" value="${t.color}"
                           oninput="this.previousElementSibling.style.background=this.value; this.previousElementSibling.style.borderColor=this.value"
                           onchange="updateTribeColor(${t.id}, this.value)"
                           class="tribe-color-input"
                           title="Pick a new color — saves on close">
                </label>
            </div>
            <button class="admin-btn btn-reset" onclick="deleteTribe(${t.id}, '${escapeHtml(t.name)}')">Remove</button>
        </div>
    `).join("");
}

async function createTribe() {
    const nameInput = document.getElementById("new-tribe-name");
    const colorInput = document.getElementById("new-tribe-color");
    const name = nameInput.value.trim();
    const color = colorInput.value;
    if (!name) { showToast("Enter a tribe name", "error"); return; }
    try {
        const res = await fetch(`/api/admin/tribes${seasonParam()}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, color }),
        });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message, "success");
            nameInput.value = "";
            colorInput.value = "#e85d26";
            await loadAdminTribes();
            renderContestantList();
        } else {
            showToast(data.detail || "Failed to create tribe", "error");
        }
    } catch (e) { showToast("Network error", "error"); }
}

async function updateTribeColor(tribeId, color) {
    try {
        const res = await fetch(`/api/admin/tribes/${tribeId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ color }),
        });
        const data = await res.json();
        if (res.ok) {
            const t = tribes.find(t => t.id === tribeId);
            if (t) t.color = color;
            injectTribeStyles(tribes);
            renderTribeManagement(tribes);
            showToast(data.message, "success");
        } else {
            showToast(data.detail || "Failed to update color", "error");
        }
    } catch (e) { showToast("Network error", "error"); }
}

async function deleteTribe(tribeId) {
    try {
        const res = await fetch(`/api/admin/tribes/${tribeId}`, { method: "DELETE" });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message, "success");
            await loadAdminTribes();
            renderContestantList();
        } else {
            showToast(data.detail || "Failed to remove tribe", "error");
        }
    } catch (e) { showToast("Network error", "error"); }
}

async function updateContestantTribe(contestantId, selectEl) {
    const tribe = selectEl.value;
    try {
        const res = await fetch(`/api/admin/contestants/${contestantId}/tribe${seasonParam()}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tribe }),
        });
        const data = await res.json();
        if (res.ok) {
            selectEl.className = `tribe-select tribe-${tribe.toLowerCase()}`;
            const c = contestants.find(c => c.id === contestantId);
            if (c) c.tribe = tribe;
            showToast(data.message, "success");
        } else {
            showToast(data.detail || "Failed to update tribe", "error");
        }
    } catch (e) {
        showToast("Network error", "error");
    }
}

async function eliminateContestant(contestantId) {
    const input = document.getElementById(`elim-order-${contestantId}`);
    const order = parseInt(input?.value);

    if (!order || order < 1 || order > contestants.length) {
        showToast(`Enter a valid elimination order (1-${contestants.length})`, "error");
        return;
    }

    try {
        const res = await fetch(`/api/admin/eliminate${seasonParam()}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ contestant_id: contestantId, elimination_order: order }),
        });

        const data = await res.json();
        if (res.ok) {
            showToast(data.message, "success");
            await loadContestants();
            renderAdminPanel();
        } else {
            showToast(data.detail || "Failed", "error");
        }
    } catch (e) {
        showToast("Network error", "error");
    }
}

async function markWinner(contestantId) {
    try {
        const res = await fetch(`/api/admin/eliminate${seasonParam()}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                contestant_id: contestantId,
                elimination_order: contestants.length,
            }),
        });

        const data = await res.json();
        if (res.ok) {
            showToast("Winner recorded! 👑", "success");
            await loadContestants();
            renderAdminPanel();
        } else {
            showToast(data.detail || "Failed", "error");
        }
    } catch (e) {
        showToast("Network error", "error");
    }
}

async function resetContestant(contestantId) {
    try {
        const res = await fetch(`/api/admin/reset-contestant${seasonParam()}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ contestant_id: contestantId }),
        });

        const data = await res.json();
        if (res.ok) {
            showToast(data.message, "success");
            await loadContestants();
            renderAdminPanel();
        } else {
            showToast(data.detail || "Failed", "error");
        }
    } catch (e) {
        showToast("Network error", "error");
    }
}

async function removeContestant(contestantId) {
    const input = document.getElementById(`elim-order-${contestantId}`);
    const order = parseInt(input?.value);

    if (!order || order < 1 || order > contestants.length) {
        showToast(`Enter a valid departure order (1-${contestants.length})`, "error");
        return;
    }

    try {
        const res = await fetch(`/api/admin/remove-contestant${seasonParam()}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ contestant_id: contestantId, elimination_order: order }),
        });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message, "success");
            await loadContestants();
            renderAdminPanel();
        } else {
            showToast(data.detail || "Failed to remove contestant", "error");
        }
    } catch (e) {
        showToast("Network error", "error");
    }
}

// --- Season Management (Admin) ---

function renderSeasonManagement() {
    const container = document.getElementById("admin-seasons-list");
    if (!container) return;

    container.innerHTML = seasons.map(s => `
        <div class="admin-item">
            <span class="contestant-name">${escapeHtml(s.name)}</span>
            <span class="admin-status ${s.is_active ? 'status-active' : 'status-eliminated'}">
                ${s.is_active ? 'Active' : 'Archived'}
            </span>
            <div class="episode-count-form">
                <label class="episode-count-label">Episodes:</label>
                <input type="number" min="1" value="${s.episode_count || ''}" placeholder="—"
                    onchange="updateEpisodeCount(${s.id}, this.value)">
            </div>
            ${!s.is_active ? `<button class="admin-btn btn-winner" onclick="activateSeason(${s.id})">Set Active</button>` : ''}
        </div>
    `).join("");
}

async function createSeason() {
    const numInput = document.getElementById("new-season-number");
    const nameInput = document.getElementById("new-season-name");
    const number = parseInt(numInput.value);
    const name = nameInput.value.trim();
    if (!number || !name) { showToast("Enter both season number and name", "error"); return; }
    try {
        const res = await fetch("/api/admin/seasons", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ number, name }),
        });
        const data = await res.json();
        if (res.ok) {
            showToast(`${data.name} created`, "success");
            numInput.value = "";
            nameInput.value = "";
            await loadSeasons();
            renderSeasonManagement();
        } else {
            showToast(data.detail || "Failed to create season", "error");
        }
    } catch (e) { showToast("Network error", "error"); }
}

async function activateSeason(seasonId) {
    try {
        const res = await fetch(`/api/admin/seasons/${seasonId}/activate`, {
            method: "POST",
        });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message, "success");
            await loadSeasons();
            await switchSeason(seasonId);
            renderSeasonManagement();
        } else {
            showToast(data.detail || "Failed to activate season", "error");
        }
    } catch (e) { showToast("Network error", "error"); }
}

async function updateEpisodeCount(seasonId, value) {
    const episodeCount = value ? parseInt(value) : null;
    try {
        const res = await fetch(`/api/admin/seasons/${seasonId}/episode-count`, {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({episode_count: episodeCount}),
        });
        if (res.ok) {
            showToast(episodeCount ? `Episode count set to ${episodeCount}` : "Episode count cleared", "success");
            await loadSeasons();
        } else {
            const err = await res.json();
            showToast(err.detail || "Failed to update", "error");
        }
    } catch (e) { showToast("Network error", "error"); }
}

// --- Database Backup/Restore ---

function exportDatabase() {
    window.location.href = "/api/admin/database/export";
}

async function importDatabase() {
    const fileInput = document.getElementById("db-import-file");
    if (!fileInput.files.length) {
        showToast("Please select a backup file first", "error");
        return;
    }

    if (!confirm("This will REPLACE all current data with the backup file. This cannot be undone.\n\nAre you sure?")) {
        return;
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);

    const btn = document.querySelector('#admin-panel-database .btn-danger');
    btn.disabled = true;
    btn.textContent = "Restoring...";

    try {
        const res = await fetch("/api/admin/database/import", {
            method: "POST",
            body: formData,
        });
        const data = await res.json();
        if (res.ok) {
            showToast("Database restored! Reloading...", "success");
            setTimeout(() => window.location.reload(), 1500);
        } else {
            showToast(data.detail || "Failed to restore database", "error");
            btn.disabled = false;
            btn.textContent = "Restore from Backup";
        }
    } catch (e) {
        showToast("Network error - try again", "error");
        btn.disabled = false;
        btn.textContent = "Restore from Backup";
    }
}

// --- Audit Log (Admin) ---

let auditUsersCache = [];

async function populateAuditUserSelect() {
    const select = document.getElementById("audit-user-select");
    if (!select) return;
    try {
        const res = await fetch(`/api/admin/users${seasonParam()}`);
        if (!res.ok) return;
        auditUsersCache = await res.json();
        select.innerHTML = `<option value="">Select a user...</option>` +
            auditUsersCache.map(u => `<option value="${u.id}">${escapeHtml(u.name)} (${escapeHtml(u.email)})</option>`).join("");
    } catch (e) {
        console.error("Failed to load audit users:", e);
    }
}

async function loadAuditLog() {
    const userId = document.getElementById("audit-user-select")?.value;
    const list = document.getElementById("audit-log-list");
    if (!userId) {
        list.innerHTML = "";
        return;
    }
    try {
        const res = await fetch(`/api/admin/audit/rankings?user_id=${userId}${seasonParam().replace("?", "&")}`);
        if (!res.ok) { list.innerHTML = `<p class="empty-state">Failed to load audit log.</p>`; return; }
        const submissions = await res.json();
        const user = auditUsersCache.find(u => u.id === parseInt(userId));
        renderAuditLog(submissions, user);
    } catch (e) {
        list.innerHTML = `<p class="empty-state">Network error loading audit log.</p>`;
    }
}

function renderAuditLog(submissions, user) {
    const list = document.getElementById("audit-log-list");
    if (submissions.length === 0) {
        list.innerHTML = `<div class="empty-state"><span class="emoji">📋</span><p>No ranking submissions found for this user in ${escapeHtml(currentSeason?.name || "this season")}.</p></div>`;
        return;
    }
    list.innerHTML = submissions.map(s => {
        const date = new Date(s.created_at + "Z");
        const timeStr = formatPacific(date);
        const sessionMismatch = user && s.session_user_email && s.session_user_email !== user.email;
        const mismatchBadge = sessionMismatch
            ? `<span class="audit-mismatch-badge" title="Session email (${escapeHtml(s.session_user_email)}) does not match user email (${escapeHtml(user.email)})">Session Mismatch</span>`
            : "";
        const ua = s.user_agent || "";
        const uaShort = ua.length > 60 ? ua.substring(0, 60) + "…" : ua;
        return `
            <div class="audit-entry ${sessionMismatch ? "audit-mismatch" : ""}">
                <div class="audit-entry-header">
                    <span class="audit-time">${escapeHtml(timeStr)}</span>
                    ${mismatchBadge}
                    <button class="admin-btn btn-sm" onclick="viewAuditSnapshot(${s.id})">View Rankings</button>
                </div>
                <div class="audit-entry-details">
                    <span class="audit-detail"><strong>IP:</strong> ${escapeHtml(s.client_ip || "unknown")}</span>
                    <span class="audit-detail"><strong>Session:</strong> ${escapeHtml(s.session_user_name || "")} (${escapeHtml(s.session_user_email || "")})</span>
                    <span class="audit-detail" title="${escapeHtml(ua)}"><strong>UA:</strong> ${escapeHtml(uaShort)}</span>
                    <span class="audit-detail"><strong>Rankings:</strong> ${s.contestant_count} contestants</span>
                </div>
            </div>`;
    }).join("");
}

async function viewAuditSnapshot(submissionId) {
    try {
        const res = await fetch(`/api/admin/audit/rankings/${submissionId}`);
        if (!res.ok) { showToast("Failed to load snapshot", "error"); return; }
        const data = await res.json();
        const date = new Date(data.created_at + "Z");

        const existing = document.getElementById("audit-modal");
        if (existing) existing.remove();

        const modal = document.createElement("div");
        modal.id = "audit-modal";
        modal.className = "modal-overlay";
        modal.innerHTML = `
            <div class="modal-box">
                <div class="modal-header">
                    <h3>Ranking Snapshot</h3>
                    <button class="modal-close" onclick="document.getElementById('audit-modal').remove()">✕</button>
                </div>
                <div class="modal-body">
                    <div class="audit-snapshot-meta">
                        <p><strong>User:</strong> ${escapeHtml(data.user_name)}</p>
                        <p><strong>Saved:</strong> ${escapeHtml(formatPacific(date))}</p>
                        <p><strong>IP:</strong> ${escapeHtml(data.client_ip || "unknown")}</p>
                        <p><strong>Session:</strong> ${escapeHtml(data.session_user_name || "")} (${escapeHtml(data.session_user_email || "")})</p>
                    </div>
                    <div class="audit-snapshot-rankings">
                        ${data.entries.map(e => `
                            <div class="modal-ranking-row">
                                <span class="modal-rank">#${e.rank}</span>
                                <span class="modal-name">${escapeHtml(e.contestant_name)}</span>
                            </div>
                        `).join("")}
                    </div>
                </div>
            </div>`;
        modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
        document.body.appendChild(modal);
    } catch (e) {
        showToast("Network error loading snapshot", "error");
    }
}

function viewUserAudit(userId) {
    document.querySelectorAll(".admin-sub-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".admin-panel-section").forEach(s => s.style.display = "none");
    document.querySelector('[data-admin-tab="audit"]').classList.add("active");
    document.getElementById("admin-panel-audit").style.display = "block";
    document.getElementById("audit-user-select").value = userId;
    loadAuditLog();
}

// --- Rankings Modal ---

async function viewUserRankings(userId, userName, apiPath) {
    try {
        const path = apiPath || `/api/admin/users/${userId}/rankings${seasonParam()}`;
        const res = await fetch(path);
        if (!res.ok) return;
        const rankings = await res.json();

        const existing = document.getElementById("rankings-modal");
        if (existing) existing.remove();

        const modal = document.createElement("div");
        modal.id = "rankings-modal";
        modal.className = "modal-overlay";
        modal.innerHTML = `
            <div class="modal-box">
                <div class="modal-header">
                    <h3>${escapeHtml(userName)}'s Rankings</h3>
                    <button class="modal-close" onclick="document.getElementById('rankings-modal').remove()">✕</button>
                </div>
                <div class="modal-body">
                    ${rankings.map(r => {
                        const statusText = r.is_winner
                            ? '👑 Winner'
                            : r.elimination_order
                                ? `Out #${r.elimination_order}`
                                : r.is_removed
                                    ? 'Removed'
                                    : 'Still playing';
                        return `
                            <div class="modal-ranking-row">
                                <span class="modal-rank">#${r.rank}</span>
                                <span class="modal-name">${escapeHtml(r.contestant_name)}</span>
                                <span class="tribe-badge tribe-${(r.tribe || '').toLowerCase()}">${escapeHtml(r.tribe || '')}</span>
                                <span class="modal-status">${statusText}</span>
                                ${r.scoring_eligible === false ? '<span class="late-badge">Late pick</span>' : ''}
                            </div>
                        `;
                    }).join("")}
                </div>
            </div>
        `;
        modal.addEventListener("click", (e) => {
            if (e.target === modal) modal.remove();
        });
        document.body.appendChild(modal);
    } catch (e) {
        showToast("Failed to load rankings", "error");
    }
}

// --- Discussion ---

async function loadDiscussionTab() {
    if (currentThread) {
        await loadThreadPosts(currentThread.id, discussionPage);
        return;
    }
    try {
        const res = await fetch(`/api/discussions${seasonParam()}`);
        if (!res.ok) return;
        discussionThreads = await res.json();
        renderEpisodeList();
    } catch (e) {
        console.error("Failed to load discussions:", e);
    }
}

function renderEpisodeList() {
    const listEl = document.getElementById("discussion-episode-list");
    const threadView = document.getElementById("discussion-thread-view");
    const adminArea = document.getElementById("discussion-admin-area");
    listEl.style.display = "block";
    threadView.style.display = "none";

    // Admin: create thread form
    if (currentUser?.is_admin && currentSeason?.is_active) {
        adminArea.innerHTML = `
            <div class="create-thread-form">
                <input type="number" id="new-episode-number" placeholder="Ep #" min="1" style="width:80px">
                <input type="text" id="new-episode-title" placeholder="Episode title (e.g. The Merge)">
                <button class="admin-btn btn-winner" onclick="createEpisodeThread()">Create Thread</button>
            </div>`;
    } else {
        adminArea.innerHTML = "";
    }

    if (!discussionThreads.length) {
        listEl.innerHTML = `<div class="empty-state"><span class="emoji">💬</span><p>No episode discussions yet.</p></div>`;
        return;
    }

    const isAdmin = currentUser?.is_admin && currentSeason?.is_active;
    listEl.innerHTML = `<div class="episode-list">${discussionThreads.map(t => `
        <div class="episode-item" onclick="openThread(${t.id})">
            <div>
                <div class="episode-title">Episode ${escapeHtml(String(t.episode_number))} &mdash; ${escapeHtml(t.title)}</div>
            </div>
            <div style="display:flex; align-items:center; gap:0.5rem;">
                <span class="episode-post-count">${t.post_count} post${t.post_count !== 1 ? "s" : ""}</span>
                ${isAdmin ? `
                    <button class="post-action-btn" onclick="event.stopPropagation(); renameThread(${t.id}, '${escapeHtml(t.title).replace(/'/g, "\\'")}')" title="Rename">Rename</button>
                    <button class="post-action-btn delete" onclick="event.stopPropagation(); deleteThread(${t.id}, ${t.episode_number})" title="Delete">Delete</button>
                ` : ""}
            </div>
        </div>
    `).join("")}</div>`;
}

async function createEpisodeThread() {
    const numInput = document.getElementById("new-episode-number");
    const titleInput = document.getElementById("new-episode-title");
    const num = parseInt(numInput.value);
    const title = titleInput.value.trim();
    if (!num || num < 1) return showToast("Enter a valid episode number", "error");
    if (!title) return showToast("Enter an episode title", "error");

    try {
        const res = await fetch(`/api/admin/discussions${seasonParam()}`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({episode_number: num, title}),
        });
        if (!res.ok) {
            const err = await res.json();
            return showToast(err.detail || "Failed to create thread", "error");
        }
        showToast("Episode thread created!", "success");
        numInput.value = "";
        titleInput.value = "";
        await loadDiscussionTab();
    } catch (e) {
        showToast("Failed to create thread", "error");
    }
}

async function renameThread(threadId, currentTitle) {
    const newTitle = prompt("Rename episode thread:", currentTitle);
    if (!newTitle || newTitle.trim() === currentTitle) return;
    try {
        const res = await fetch(`/api/admin/discussions/${threadId}`, {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({title: newTitle.trim()}),
        });
        if (!res.ok) {
            const err = await res.json();
            return showToast(err.detail || "Failed to rename", "error");
        }
        showToast("Thread renamed", "success");
        await loadDiscussionTab();
    } catch (e) {
        showToast("Failed to rename thread", "error");
    }
}

async function deleteThread(threadId, episodeNumber) {
    if (!confirm(`Delete Episode ${episodeNumber} thread and all its posts? This cannot be undone.`)) return;
    try {
        const res = await fetch(`/api/admin/discussions/${threadId}`, {method: "DELETE"});
        if (!res.ok) {
            const err = await res.json();
            return showToast(err.detail || "Failed to delete", "error");
        }
        showToast("Thread deleted", "success");
        await loadDiscussionTab();
    } catch (e) {
        showToast("Failed to delete thread", "error");
    }
}

async function openThread(threadId) {
    currentThread = discussionThreads.find(t => t.id === threadId);
    discussionPage = 1;
    await loadThreadPosts(threadId, 1);
}

async function loadThreadPosts(threadId, page) {
    try {
        const res = await fetch(`/api/discussions/${threadId}/posts?page=${page}`);
        if (!res.ok) return;
        const data = await res.json();
        currentThreadPosts = data.posts;
        discussionPage = data.page;
        discussionTotalPages = data.total_pages;
        renderThreadView(data);
    } catch (e) {
        console.error("Failed to load posts:", e);
    }
}

function renderThreadView(data) {
    const listEl = document.getElementById("discussion-episode-list");
    const threadView = document.getElementById("discussion-thread-view");
    const adminArea = document.getElementById("discussion-admin-area");
    listEl.style.display = "none";
    adminArea.innerHTML = "";
    threadView.style.display = "block";

    const isActive = currentSeason?.is_active;
    const posts = data.posts;

    let html = `<button class="back-btn" onclick="backToEpisodeList()">&larr; Back to episodes</button>`;
    html += `<h3 style="margin-bottom:1rem;">Episode ${escapeHtml(String(currentThread.episode_number))} &mdash; ${escapeHtml(currentThread.title)}</h3>`;

    // Compose area (top, only for active season + logged in)
    if (currentUser && isActive) {
        html += `
            <div class="post-compose">
                <textarea id="post-content" maxlength="500" placeholder="Share your thoughts on this episode..."></textarea>
                <div class="char-counter" id="char-counter">0 / 500</div>
                <button class="primary-btn" style="margin-top:0.5rem;" onclick="submitPost(${currentThread.id})">Post</button>
            </div>`;
    }

    if (!posts.length) {
        html += `<div class="empty-state"><p>No posts yet. Be the first to share your thoughts!</p></div>`;
    } else {
        html += posts.map(p => renderPost(p, isActive)).join("");
    }

    // Pagination
    if (data.total_pages > 1) {
        html += `
            <div class="pagination-controls">
                <button ${data.page <= 1 ? "disabled" : ""} onclick="loadThreadPosts(${currentThread.id}, ${data.page - 1})">Previous</button>
                <span class="pagination-info">Page ${data.page} of ${data.total_pages}</span>
                <button ${data.page >= data.total_pages ? "disabled" : ""} onclick="loadThreadPosts(${currentThread.id}, ${data.page + 1})">Next</button>
            </div>`;
    }

    threadView.innerHTML = html;

    // Set up character counter
    const textarea = document.getElementById("post-content");
    if (textarea) {
        textarea.addEventListener("input", () => {
            const counter = document.getElementById("char-counter");
            const len = textarea.value.length;
            counter.textContent = `${len} / 500`;
            counter.className = "char-counter" + (len >= 500 ? " at-limit" : len >= 400 ? " near-limit" : "");
        });
    }
}

function renderPost(post, isActive) {
    const isOwn = currentUser && currentUser.user_id === post.user_id;
    const isAdmin = currentUser?.is_admin;
    const time = new Date(post.created_at);
    const timeStr = formatPacific(time, { year: undefined, timeZoneName: undefined });

    const avatarSrc = post.user_picture ? escapeHtml(post.user_picture) : "";
    const avatarHtml = avatarSrc
        ? `<img class="post-avatar" src="${avatarSrc}" alt="" referrerpolicy="no-referrer">`
        : `<div class="post-avatar" style="background:var(--border);"></div>`;

    const editedBadge = post.is_edited ? ` <span class="post-edited">(edited)</span>` : "";

    const reactionHtml = ["like", "heart", "sad"].map(type => {
        const emoji = type === "like" ? "👍" : type === "heart" ? "❤️" : "😢";
        const count = post.reactions[type] || 0;
        const active = (post.user_reactions || []).includes(type) ? " active" : "";
        const clickable = currentUser && isActive;
        const onclick = clickable ? `onclick="toggleReaction(${post.id}, '${type}')"` : "";
        return `<button class="reaction-btn${active}" ${onclick}${!clickable ? " disabled" : ""}>${emoji} ${count}</button>`;
    }).join("");

    let actionsHtml = reactionHtml;
    if (isOwn && isActive) {
        actionsHtml += ` <button class="post-action-btn" onclick="startEditPost(${post.id})">Edit</button>`;
    }
    if (isAdmin) {
        actionsHtml += ` <button class="post-action-btn delete" onclick="deletePost(${post.id})">Delete</button>`;
    }

    return `
        <div class="discussion-post" id="post-${post.id}">
            <div class="post-header">
                ${avatarHtml}
                <span class="post-author">${escapeHtml(post.display_name)}</span>
                <span class="post-time">${timeStr}${editedBadge}</span>
            </div>
            <div class="post-content" id="post-content-${post.id}">${escapeHtml(post.content)}</div>
            <div class="post-actions" id="post-actions-${post.id}">${actionsHtml}</div>
        </div>`;
}

async function submitPost(threadId) {
    const textarea = document.getElementById("post-content");
    const content = textarea.value.trim();
    if (!content) return showToast("Write something first!", "error");
    if (content.length > 500) return showToast("Post is too long (500 char max)", "error");

    try {
        const res = await fetch(`/api/discussions/${threadId}/posts${seasonParam()}`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({content}),
        });
        if (!res.ok) {
            const err = await res.json();
            return showToast(err.detail || "Failed to post", "error");
        }
        // Reload the last page to see the new post
        const totalRes = await fetch(`/api/discussions/${threadId}/posts?page=999`);
        const totalData = await totalRes.json();
        await loadThreadPosts(threadId, totalData.page);
    } catch (e) {
        showToast("Failed to post", "error");
    }
}

function startEditPost(postId) {
    const post = currentThreadPosts.find(p => p.id === postId);
    if (!post) return;

    const contentEl = document.getElementById(`post-content-${postId}`);
    const actionsEl = document.getElementById(`post-actions-${postId}`);

    contentEl.innerHTML = `
        <div class="post-edit-area">
            <textarea id="edit-textarea-${postId}" maxlength="500">${escapeHtml(post.content)}</textarea>
            <div class="char-counter" id="edit-counter-${postId}">${post.content.length} / 500</div>
        </div>`;

    actionsEl.innerHTML = `
        <div class="post-edit-actions">
            <button class="save-edit" onclick="saveEditPost(${postId})">Save</button>
            <button class="cancel-edit" onclick="loadThreadPosts(${currentThread.id}, ${discussionPage})">Cancel</button>
        </div>`;

    const editArea = document.getElementById(`edit-textarea-${postId}`);
    editArea.addEventListener("input", () => {
        const counter = document.getElementById(`edit-counter-${postId}`);
        const len = editArea.value.length;
        counter.textContent = `${len} / 500`;
        counter.className = "char-counter" + (len >= 500 ? " at-limit" : len >= 400 ? " near-limit" : "");
    });
    editArea.focus();
}

async function saveEditPost(postId) {
    const textarea = document.getElementById(`edit-textarea-${postId}`);
    const content = textarea.value.trim();
    if (!content) return showToast("Post cannot be empty", "error");
    if (content.length > 500) return showToast("Post is too long (500 char max)", "error");

    try {
        const res = await fetch(`/api/discussions/posts/${postId}`, {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({content}),
        });
        if (!res.ok) {
            const err = await res.json();
            return showToast(err.detail || "Failed to edit", "error");
        }
        showToast("Post updated", "success");
        await loadThreadPosts(currentThread.id, discussionPage);
    } catch (e) {
        showToast("Failed to edit post", "error");
    }
}

async function deletePost(postId) {
    if (!confirm("Delete this post?")) return;
    try {
        const res = await fetch(`/api/admin/discussions/posts/${postId}`, {method: "DELETE"});
        if (!res.ok) {
            const err = await res.json();
            return showToast(err.detail || "Failed to delete", "error");
        }
        showToast("Post deleted", "success");
        await loadThreadPosts(currentThread.id, discussionPage);
    } catch (e) {
        showToast("Failed to delete post", "error");
    }
}

async function toggleReaction(postId, reactionType) {
    try {
        const res = await fetch(`/api/discussions/posts/${postId}/reactions`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({reaction_type: reactionType}),
        });
        if (!res.ok) return;
        const data = await res.json();

        // Update the post in local state and re-render just the actions
        const post = currentThreadPosts.find(p => p.id === postId);
        if (post) {
            post.reactions = data.reactions;
            post.user_reactions = data.user_reactions;

            const actionsEl = document.getElementById(`post-actions-${postId}`);
            if (actionsEl) {
                const isActive = currentSeason?.is_active;
                const isOwn = currentUser && currentUser.user_id === post.user_id;
                const isAdmin = currentUser?.is_admin;

                let actionsHtml = ["like", "heart", "sad"].map(type => {
                    const emoji = type === "like" ? "👍" : type === "heart" ? "❤️" : "😢";
                    const count = data.reactions[type] || 0;
                    const active = (data.user_reactions || []).includes(type) ? " active" : "";
                    return `<button class="reaction-btn${active}" onclick="toggleReaction(${postId}, '${type}')">${emoji} ${count}</button>`;
                }).join("");
                if (isOwn && isActive) {
                    actionsHtml += ` <button class="post-action-btn" onclick="startEditPost(${postId})">Edit</button>`;
                }
                if (isAdmin) {
                    actionsHtml += ` <button class="post-action-btn delete" onclick="deletePost(${postId})">Delete</button>`;
                }
                actionsEl.innerHTML = actionsHtml;
            }
        }
    } catch (e) {
        console.error("Failed to toggle reaction:", e);
    }
}

function backToEpisodeList() {
    currentThread = null;
    currentThreadPosts = [];
    discussionPage = 1;
    loadDiscussionTab();
}

// --- What's New ---

const WHATS_NEW = [
    {
        version: "v30",
        title: "Bug Fixes & Improvements",
        description: "Timestamps now display in Pacific time. Various behind-the-scenes stability and performance improvements.",
    },
    {
        version: "v25",
        title: "Email Your Rankings",
        description: "Your rankings are now emailed to you automatically when you save. You can also click 'Email to Me' anytime to get a formatted copy in your inbox.",
    },
    {
        version: "v24",
        title: "What's New Modal",
        description: "Click the version number in the footer to see what's changed in each update.",
    },
    {
        version: "v23",
        title: "Multiple Reactions",
        description: "You can now react to discussion posts with any combination of reactions. Previously you could only pick one.",
    },
    {
        version: "v22",
        title: "Episode Discussion",
        description: "Talk about each episode with other players! Open a thread, share your thoughts, edit your posts, and react to others.",
    },
    {
        version: "v21",
        title: "Pull to Refresh",
        description: "Swipe down from the top of the screen to refresh data when using the app on your phone.",
    },
    {
        version: "v20",
        title: "Install as App",
        description: "Add Survivor Rankings to your phone's home screen for a native app experience. Also fixed a scrolling glitch on iPhone.",
    },
    {
        version: "v19",
        title: "Season Selector",
        description: "Switch between seasons using the dropdown in the footer.",
    },
    {
        version: "v18",
        title: "Multiple Seasons",
        description: "The app now supports multiple seasons. Past seasons are viewable but read-only.",
    },
];

function showWhatsNew() {
    const existing = document.getElementById("whats-new-modal");
    if (existing) existing.remove();

    const modal = document.createElement("div");
    modal.id = "whats-new-modal";
    modal.className = "modal-overlay";
    modal.innerHTML = `
        <div class="modal-box">
            <div class="modal-header">
                <h3>What's New</h3>
                <button class="modal-close" onclick="document.getElementById('whats-new-modal').remove()">&#10005;</button>
            </div>
            <div class="modal-body">
                ${WHATS_NEW.map(entry => `
                    <div style="margin-bottom:1.2rem;">
                        <div style="font-weight:700; font-size:0.95rem; color:var(--primary); margin-bottom:0.2rem;">${escapeHtml(entry.version)} &mdash; ${escapeHtml(entry.title)}</div>
                        <div style="font-size:0.9rem; color:var(--text); line-height:1.5;">${escapeHtml(entry.description)}</div>
                    </div>
                `).join("")}
            </div>
        </div>
    `;
    modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.remove();
    });
    document.body.appendChild(modal);
}

// --- Toast ---

function showToast(message, type = "info") {
    const existing = document.querySelector(".toast");
    if (existing) existing.remove();

    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => toast.remove(), 3000);
}

// --- Util ---
function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function formatPacific(date, opts = {}) {
    const defaults = { timeZone: "America/Los_Angeles", month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" };
    return date.toLocaleString("en-US", { ...defaults, ...opts });
}
