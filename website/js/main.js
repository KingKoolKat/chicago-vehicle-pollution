document.body.insertAdjacentHTML('afterbegin', createNavigation());
const CHAT_API_URL = window.CHAT_API_URL || '/chat';
const CHAT_TEST_MODE = window.CHAT_TEST_MODE === true;
const TRAFFIC_MAP_API_URL = window.TRAFFIC_MAP_API_URL || '/traffic_map';
const isHomePage = window.location.pathname === '/' || window.location.pathname.endsWith('/index.html');
let sectionsReadyPromise = Promise.resolve();
let userLocation = null;
const LAST_SECTION_KEY = 'ecotrack_last_section';
const TRACKED_SECTION_IDS = ['top', 'heatmap', 'predictions', 'upload', 'chat'];
let sectionObserver = null;

function initUserMenu() {
    const trigger = document.getElementById('userMenuTrigger');
    const dropdown = document.getElementById('userMenuDropdown');
    const logoutBtn = document.getElementById('logoutBtn');
    const container = document.getElementById('userMenuContainer');

    if (!trigger || !dropdown || !container) return;
    if (trigger.dataset.bound === 'true') return;
    trigger.dataset.bound = 'true';

    trigger.addEventListener('click', (event) => {
        event.stopPropagation();
        dropdown.classList.toggle('hidden');
    });

    document.addEventListener('click', (event) => {
        if (!container.contains(event.target)) {
            dropdown.classList.add('hidden');
        }
    });

    if (logoutBtn) {
        logoutBtn.addEventListener('click', () => {
            if (window.Auth) {
                window.Auth.logout();
            }
            window.location.href = isHomePage ? window.location.pathname : '../';
        });
    }
}

initUserMenu();

function applyTranslations(lang) {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (translations[lang] && translations[lang][key]) {
            el.textContent = translations[lang][key];
        }
    });
}

// Language Switcher
document.getElementById('languageSelect').addEventListener('change', (e) => {
    applyTranslations(e.target.value);
});

function waitForElementById(id, retries = 30, delayMs = 100) {
    return new Promise((resolve) => {
        const existing = document.getElementById(id);
        if (existing) {
            resolve(existing);
            return;
        }

        let attempts = 0;
        const timer = setInterval(() => {
            const node = document.getElementById(id);
            if (node || attempts >= retries) {
                clearInterval(timer);
                resolve(node || null);
            }
            attempts += 1;
        }, delayMs);
    });
}

function persistSection(id) {
    if (!id) return;
    sessionStorage.setItem(LAST_SECTION_KEY, id);
}

function getRequestedSection() {
    const url = new URL(window.location.href);
    const querySection = url.searchParams.get('section');
    const hashSection = url.hash ? url.hash.replace('#', '') : null;
    return querySection || hashSection || sessionStorage.getItem(LAST_SECTION_KEY) || 'top';
}

function observeVisibleSections() {
    if (!isHomePage) return;

    if (sectionObserver) {
        sectionObserver.disconnect();
    }

    const observed = TRACKED_SECTION_IDS
        .map((id) => document.getElementById(id))
        .filter(Boolean);

    if (observed.length === 0) return;

    sectionObserver = new IntersectionObserver((entries) => {
        const visible = entries
            .filter((entry) => entry.isIntersecting)
            .sort((a, b) => b.intersectionRatio - a.intersectionRatio);

        if (visible.length > 0) {
            persistSection(visible[0].target.id);
        }
    }, { threshold: [0.45, 0.7] });

    observed.forEach((el) => sectionObserver.observe(el));
}

async function injectSectionsOnHomePage() {
    if (!isHomePage) return;

    const sectionTargets = [
        { url: 'heat-map/index.html', selector: '#heatmap', targetId: 'mapSection' },
        { url: 'heat-map/index.html', selector: '#predictions', targetId: 'chartSection' },
        { url: 'chatbot/index.html', selector: '#chat', targetId: 'chatSection' }
    ];

    const parser = new DOMParser();

    for (const section of sectionTargets) {
        const target = document.getElementById(section.targetId);
        if (!target) continue;

        try {
            const response = await fetch(section.url);
            if (!response.ok) continue;

            const html = await response.text();
            const doc = parser.parseFromString(html, 'text/html');
            const sourceSection = doc.querySelector(section.selector);
            if (sourceSection) {
                target.insertAdjacentHTML('beforeend', sourceSection.outerHTML);
            }
        } catch (error) {
            console.error(`Failed to inject ${section.url} (${section.selector})`, error);
        }
    }
}

async function scrollToSection(id, { behavior = 'smooth', persist = true } = {}) {
    await sectionsReadyPromise;
    const target = await waitForElementById(id);
    if (target) {
        if (persist) {
            persistSection(id);
        }
        target.scrollIntoView({ behavior, block: 'start' });
    } else if (id === 'upload') {
        window.location.href = 'upload/';
    }
}

function observeVisibleSections() {
    if (!isHomePage) return;

    if (sectionObserver) {
        sectionObserver.disconnect();
    }

    const observed = TRACKED_SECTION_IDS
        .map((id) => document.getElementById(id))
        .filter(Boolean);

    if (observed.length === 0) return;

    sectionObserver = new IntersectionObserver((entries) => {
        const visible = entries
            .filter((entry) => entry.isIntersecting)
            .sort((a, b) => b.intersectionRatio - a.intersectionRatio);

        if (visible.length > 0) {
            const id = visible[0].target.id;
            persistSection(id);
            
            // Update URL when section becomes visible from scrolling (not from click)
            const url = new URL(window.location.href);
            if (url.searchParams.get('section') !== id) {
                url.searchParams.set('section', id);
                window.history.replaceState({}, '', url);
            }
        }
    }, { threshold: [0.45, 0.7] });

    observed.forEach((el) => sectionObserver.observe(el));
}

function handleUploadClick() {
    const currentUser = window.Auth ? window.Auth.getSessionUser() : null;
    if (currentUser) {
        window.location.href = 'upload/?section=upload';
    } else {
        sessionStorage.setItem('redirectAfterLogin', 'upload/?section=upload');
        window.location.href = 'login/';
    }
}

// Initialize Map
let map;
let trafficMarkers = [];
let trafficDates = [];
let selectedTrafficDate = null;
const trafficPopupCharts = {};
let pulseMarkers = [];
let cameraJumpPoints = [];
let activeCameraJumpId = null;
let mapArrowNavigationBound = false;
const PULSE_RADIUS_DIVISOR = 57;
const CAMERA_HIT_RADIUS_PX = 14;
const HEATMAP_CONFIG = window.HEATMAP_CONFIG || {};
const HEATMAP_POLLUTANTS = HEATMAP_CONFIG.POLLUTANTS || {};
const HEATMAP_DEFAULT_POLLUTANT = HEATMAP_CONFIG.DEFAULT_POLLUTANT || 'CO2';
const HEATMAP_DEFAULT_METRIC = HEATMAP_CONFIG.DEFAULT_METRIC || 'total';
const HEATMAP_CONGESTION_K = Number(HEATMAP_CONFIG.CONGESTION_MULTIPLIER_K || 0.5);
const HEATMAP_COLOR_PALETTES = HEATMAP_CONFIG.COLOR_PALETTES || {};
const HEATMAP_RADIUS_CONFIG = HEATMAP_CONFIG.DIFFUSION_RADIUS_METERS || {};
const HEATMAP_TRAFFIC_RADIUS_METERS = Math.max(100, Number(HEATMAP_RADIUS_CONFIG.traffic || 300));
const HEATMAP_EMISSIONS_RADIUS_METERS = Math.max(100, Number(HEATMAP_RADIUS_CONFIG.emissions || 1200));

let heatmapMode = 'traffic';
let selectedPollutant = HEATMAP_DEFAULT_POLLUTANT;
let selectedEmissionMetric = HEATMAP_DEFAULT_METRIC === 'intensity' ? 'intensity' : 'total';
let latestTrafficPayload = null;
const emissionsCongestionCache = new Map();

function setTrafficMapStatus(message, isError = false) {
    const status = document.getElementById('trafficMapStatus');
    if (!status) return;
    status.textContent = message;
    status.classList.toggle('text-red-500', isError);
}

function formatCount(value) {
    const numeric = Number(value || 0);
    if (!Number.isFinite(numeric)) return '0';
    return numeric.toLocaleString();
}

function formatMeasure(value, fractionDigits = 2) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '0';
    return numeric.toLocaleString(undefined, {
        minimumFractionDigits: 0,
        maximumFractionDigits: fractionDigits,
    });
}

function toSafeNumber(value, fallback = 0) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : fallback;
}

function formatDashboardEmissionKg(totalKg) {
    const numeric = toSafeNumber(totalKg, 0);
    if (numeric >= 1000) return formatMeasure(numeric, 0);
    if (numeric >= 100) return formatMeasure(numeric, 1);
    return formatMeasure(numeric, 2);
}

function computeSummaryEmissionTotal(summary, pollutant) {
    const meta = resolvePollutantConfig(pollutant, HEATMAP_POLLUTANTS);
    const car = toSafeNumber(summary?.car_count, 0);
    const truck = toSafeNumber(summary?.truck_count, 0);
    const bus = toSafeNumber(summary?.bus_count, 0);
    const motorcycle = toSafeNumber(summary?.motorcycle_count, 0);

    const totalRaw = (car * meta.factors.car)
        + (truck * meta.factors.truck)
        + (bus * meta.factors.bus)
        + (motorcycle * meta.factors.motorcycle);

    return totalRaw * meta.totalDisplayScale;
}

function updateHeroStatsFromTrafficPayload(payload) {
    const vehiclesNode = document.getElementById('vehiclesYesterday');
    const co2Node = document.getElementById('co2Yesterday');
    const noxNode = document.getElementById('noxYesterday');
    const pm25Node = document.getElementById('pm25Yesterday');
    if (!vehiclesNode || !co2Node || !noxNode || !pm25Node) return;

    const summary = payload?.summary || {};
    vehiclesNode.textContent = formatCount(summary.total_unique_vehicles);
    co2Node.textContent = formatDashboardEmissionKg(computeSummaryEmissionTotal(summary, 'CO2'));
    noxNode.textContent = formatDashboardEmissionKg(computeSummaryEmissionTotal(summary, 'NOx'));
    pm25Node.textContent = formatDashboardEmissionKg(computeSummaryEmissionTotal(summary, 'PM2.5'));
}

function clamp01(value) {
    return Math.max(0, Math.min(1, Number(value) || 0));
}

function percentileFromSorted(sortedValues, percentile) {
    if (!Array.isArray(sortedValues) || sortedValues.length === 0) return 0;
    if (sortedValues.length === 1) return toSafeNumber(sortedValues[0], 0);

    const p = clamp01(percentile);
    const index = (sortedValues.length - 1) * p;
    const lower = Math.floor(index);
    const upper = Math.ceil(index);
    const lowerValue = toSafeNumber(sortedValues[lower], 0);
    const upperValue = toSafeNumber(sortedValues[upper], lowerValue);

    if (lower === upper) return lowerValue;
    const weight = index - lower;
    return lowerValue + (upperValue - lowerValue) * weight;
}

function trafficLevelFromIntensity(intensity) {
    const clamped = clamp01(intensity);
    if (clamped >= 0.67) return 'heavy';
    if (clamped >= 0.34) return 'moderate';
    return 'light';
}

function colorFromIntensity(intensity) {
    const clamped = clamp01(intensity);
    const palette = resolveCurrentColorPalette();

    if (clamped > 0.7) return palette.high;
    if (clamped > 0.4) return palette.medium;
    return palette.low;
}

function resolveCurrentColorPalette() {
    const trafficFallback = {
        low: '#00ff88',
        medium: '#ffaa00',
        high: '#ff3366',
    };
    const emissionsFallback = {
        low: '#39c5ff',
        medium: '#8b5cf6',
        high: '#ff4fd8',
    };

    const modeFallback = heatmapMode === 'emissions' ? emissionsFallback : trafficFallback;
    const configuredPalette = heatmapMode === 'emissions'
        ? (HEATMAP_COLOR_PALETTES.emissions || {})
        : (HEATMAP_COLOR_PALETTES.traffic || {});

    return {
        low: configuredPalette.low || modeFallback.low,
        medium: configuredPalette.medium || modeFallback.medium,
        high: configuredPalette.high || modeFallback.high,
    };
}

function currentDiffusionRadiusMeters() {
    return heatmapMode === 'emissions'
        ? HEATMAP_EMISSIONS_RADIUS_METERS
        : HEATMAP_TRAFFIC_RADIUS_METERS;
}

function heatmapLegendUnitLabel() {
    if (heatmapMode !== 'emissions') return 'veh';
    const pollutantMeta = resolvePollutantConfig(selectedPollutant, HEATMAP_POLLUTANTS);
    return selectedEmissionMetric === 'intensity' ? pollutantMeta.intensityUnit : pollutantMeta.totalUnit;
}

function formatHeatmapLegendValue(value) {
    const numeric = toSafeNumber(value, 0);
    if (heatmapMode !== 'emissions') {
        return formatCount(Math.round(numeric));
    }
    if (selectedEmissionMetric === 'intensity') {
        return formatMeasure(numeric, 2);
    }
    const pollutantMeta = resolvePollutantConfig(selectedPollutant, HEATMAP_POLLUTANTS);
    return formatDashboardEmissionKg(numeric * pollutantMeta.totalDisplayScale);
}

function updateHeatmapLegend(scale = null) {
    const lowScaleLabel = document.getElementById('legendScaleLow');
    const highScaleLabel = document.getElementById('legendScaleHigh');
    const gradientBar = document.querySelector('.heatmap-legend');
    const unit = heatmapLegendUnitLabel();
    const hasScale = scale
        && Number.isFinite(scale.p05)
        && Number.isFinite(scale.p95);

    if (lowScaleLabel) {
        lowScaleLabel.textContent = hasScale
            ? `P5: ${formatHeatmapLegendValue(scale.p05)} ${unit}`
            : `P5: -- ${unit}`;
    }
    if (highScaleLabel) {
        highScaleLabel.textContent = hasScale
            ? `P95: ${formatHeatmapLegendValue(scale.p95)} ${unit}`
            : `P95: -- ${unit}`;
    }

    if (gradientBar) {
        const palette = resolveCurrentColorPalette();
        gradientBar.style.background = `linear-gradient(to right, ${palette.low} 0%, ${palette.medium} 55%, ${palette.high} 100%)`;
    }
}

function clearTrafficMarkers() {
    Object.keys(trafficPopupCharts).forEach((canvasId) => destroyPopupChart(canvasId));
    trafficMarkers.forEach((layer) => {
        if (map && map.hasLayer(layer)) {
            map.removeLayer(layer);
        }
    });
    trafficMarkers = [];
    pulseMarkers = [];
    cameraJumpPoints = [];
}

function updatePulseMarkers() {
    if (!map) return;
    const currentZoom = map.getZoom();

    pulseMarkers.forEach((pulse) => {
        const scale = map.getZoomScale(currentZoom, pulse.baseZoom);
        const newRadius = (toSafeNumber(pulse.circleRadius) / PULSE_RADIUS_DIVISOR) * scale;
        const element = pulse.marker.getElement();
        if (!element) return;

        const ring = element.querySelector('.pulse-ring');
        if (!ring) return;

        ring.style.width = `${newRadius}px`;
        ring.style.height = `${newRadius}px`;
        ring.style.top = `-${newRadius / 2}px`;
        ring.style.left = `-${newRadius / 2}px`;
    });
}

function updateTrafficSummary(summary) {
    const heavyCount = document.getElementById('heavyCameraCount');
    const totalVehicles = document.getElementById('totalVehiclesCount');
    const camerasReporting = document.getElementById('cameraReportingCount');
    const heavyLabel = document.getElementById('heavyCameraLabel');
    const totalLabel = document.getElementById('totalMetricLabel');
    const totalDescription = document.getElementById('totalMetricDescription');

    if (heavyLabel && summary.heavyLabel) {
        heavyLabel.textContent = summary.heavyLabel;
    }
    if (totalLabel && summary.metricLabel) {
        totalLabel.textContent = summary.metricLabel;
    }
    if (totalDescription && summary.metricDescription) {
        totalDescription.textContent = summary.metricDescription;
    }

    if (heavyCount) heavyCount.textContent = formatCount(summary.heavy_count);
    if (totalVehicles) {
        if (summary.metricIsFloat) {
            totalVehicles.textContent = formatMeasure(summary.metricValue, 2);
        } else {
            totalVehicles.textContent = formatCount(summary.metricValue);
        }
    }
    if (camerasReporting) camerasReporting.textContent = formatCount(summary.camera_count);
}

function updateTrafficDateControls(selectedDate, availableDates) {
    trafficDates = Array.isArray(availableDates) ? availableDates : [];
    selectedTrafficDate = selectedDate || null;

    const label = document.getElementById('trafficDateLabel');
    const slider = document.getElementById('trafficDateSlider');
    const minDate = document.getElementById('trafficDateMin');
    const maxDate = document.getElementById('trafficDateMax');

    if (label) {
        label.textContent = selectedTrafficDate || 'No data';
    }

    if (!slider || trafficDates.length === 0) {
        if (slider) slider.disabled = true;
        if (minDate) minDate.textContent = '--';
        if (maxDate) maxDate.textContent = '--';
        return;
    }

    const selectedIndex = Math.max(0, trafficDates.indexOf(selectedTrafficDate));
    slider.min = '0';
    slider.max = String(trafficDates.length - 1);
    slider.step = '1';
    slider.value = String(selectedIndex);
    slider.disabled = false;

    if (minDate) minDate.textContent = trafficDates[0];
    if (maxDate) maxDate.textContent = trafficDates[trafficDates.length - 1];

    if (slider.dataset.bound !== 'true') {
        slider.dataset.bound = 'true';
        slider.addEventListener('input', (event) => {
            const idx = Number(event.target.value);
            if (label && trafficDates[idx]) {
                label.textContent = trafficDates[idx];
            }
        });
        slider.addEventListener('change', async (event) => {
            const idx = Number(event.target.value);
            const date = trafficDates[idx];
            if (date) {
                await loadTrafficMap(date);
            }
        });
    }
}

function destroyPopupChart(canvasId) {
    if (trafficPopupCharts[canvasId]) {
        trafficPopupCharts[canvasId].destroy();
        delete trafficPopupCharts[canvasId];
    }
}

function renderVehiclePieChart(canvasId, camera) {
    if (typeof Chart === 'undefined') return;

    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    destroyPopupChart(canvasId);

    const values = [
        Number(camera.car_count || 0),
        Number(camera.bus_count || 0),
        Number(camera.truck_count || 0),
        Number(camera.motorcycle_count || 0),
    ];

    const total = values.reduce((sum, value) => sum + value, 0);
    if (total <= 0) return;

    trafficPopupCharts[canvasId] = new Chart(canvas, {
        type: 'pie',
        data: {
            labels: ['Cars', 'Buses', 'Trucks', 'Motorcycles'],
            datasets: [{
                data: values,
                backgroundColor: ['#60a5fa', '#f59e0b', '#ef4444', '#22c55e'],
                borderColor: ['#1d4ed8', '#b45309', '#b91c1c', '#15803d'],
                borderWidth: 1,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        boxWidth: 10,
                        color: '#374151',
                        font: {
                            size: 10,
                        },
                    },
                },
                tooltip: {
                    callbacks: {
                        label(context) {
                            const value = Number(context.raw || 0);
                            const pct = total > 0 ? ((value / total) * 100).toFixed(1) : '0.0';
                            return `${context.label}: ${formatCount(value)} (${pct}%)`;
                        },
                    },
                },
            },
        },
    });
}

function congestionCacheKey(dateKey, k) {
    return `${dateKey || 'no-date'}::k=${k}`;
}

function buildCongestionCacheForDate(dateKey, cameras, k = HEATMAP_CONGESTION_K) {
    const key = congestionCacheKey(dateKey, k);
    const existing = emissionsCongestionCache.get(key);
    if (existing) return existing;

    const peakValues = cameras
        .map((camera) => {
            const peak = Number(camera.peak_vehicles_per_frame);
            return Number.isFinite(peak) ? peak : null;
        })
        .filter((value) => value !== null)
        .sort((a, b) => a - b);

    const p10 = peakValues.length > 0 ? percentileFromSorted(peakValues, 0.10) : 0;
    const p90 = peakValues.length > 0 ? percentileFromSorted(peakValues, 0.90) : 0;
    const hasSpread = Number.isFinite(p10) && Number.isFinite(p90) && p90 > p10;
    const byCamera = {};

    cameras.forEach((camera) => {
        const cameraKey = String(camera.camera_id ?? '');
        const peak = Number(camera.peak_vehicles_per_frame);
        let z = 0;
        let alpha = 1;

        if (Number.isFinite(peak) && hasSpread) {
            z = clamp01((peak - p10) / (p90 - p10));
            alpha = 1 + (k * z);
        }

        byCamera[cameraKey] = { z, alpha };
    });

    const cacheEntry = {
        key,
        dateKey,
        p10,
        p90,
        k,
        byCamera,
    };
    emissionsCongestionCache.set(key, cacheEntry);
    return cacheEntry;
}

function resolvePollutantConfig(pollutant, EF = HEATMAP_POLLUTANTS) {
    const pollutantConfig = EF[pollutant] || {};
    const factors = pollutantConfig.emissionFactors || {};
    return {
        pollutant,
        label: pollutantConfig.label || pollutant,
        totalUnit: pollutantConfig.totalUnit || 'kg/day',
        intensityUnit: pollutantConfig.intensityUnit || 'g/vehicle',
        totalDisplayScale: toSafeNumber(pollutantConfig.totalDisplayScale, 0.001),
        factors: {
            car: toSafeNumber(factors.car, 0),
            truck: toSafeNumber(factors.truck, 0),
            bus: toSafeNumber(factors.bus, 0),
            motorcycle: toSafeNumber(factors.motorcycle, 0),
        },
    };
}

/*
Emissions model per camera/date:
- z = clamp((P - P10) / (P90 - P10), 0, 1)
- alpha = 1 + k*z
- base = Ncar*EFcar + Ntruck*EFtruck + Nbus*EFbus + Nmoto*EFmoto
- E_total = base * alpha
- E_intensity = E_total / max(N_total, 1)
*/
function computeEmissionsWeights(cameras, options = {}) {
    const pollutant = options.pollutant || selectedPollutant;
    const metric = options.metric === 'intensity' ? 'intensity' : 'total';
    const k = Number.isFinite(Number(options.k)) ? Number(options.k) : HEATMAP_CONGESTION_K;
    const EF = options.EF || HEATMAP_POLLUTANTS;
    const dateKey = options.dateKey || selectedTrafficDate || 'no-date';
    const pollutantConfig = resolvePollutantConfig(pollutant, EF);
    const congestionCache = buildCongestionCacheForDate(dateKey, cameras, k);

    return cameras.map((camera) => {
        const counts = {
            car: toSafeNumber(camera.car_count, 0),
            truck: toSafeNumber(camera.truck_count, 0),
            bus: toSafeNumber(camera.bus_count, 0),
            motorcycle: toSafeNumber(camera.motorcycle_count, 0),
        };

        const cameraKey = String(camera.camera_id ?? '');
        const congestion = congestionCache.byCamera[cameraKey] || { z: 0, alpha: 1 };

        const baseBreakdown = {
            car: counts.car * pollutantConfig.factors.car,
            truck: counts.truck * pollutantConfig.factors.truck,
            bus: counts.bus * pollutantConfig.factors.bus,
            motorcycle: counts.motorcycle * pollutantConfig.factors.motorcycle,
        };

        const adjustedBreakdown = {
            car: baseBreakdown.car * congestion.alpha,
            truck: baseBreakdown.truck * congestion.alpha,
            bus: baseBreakdown.bus * congestion.alpha,
            motorcycle: baseBreakdown.motorcycle * congestion.alpha,
        };
        const adjustedBreakdownDisplay = {
            car: adjustedBreakdown.car * pollutantConfig.totalDisplayScale,
            truck: adjustedBreakdown.truck * pollutantConfig.totalDisplayScale,
            bus: adjustedBreakdown.bus * pollutantConfig.totalDisplayScale,
            motorcycle: adjustedBreakdown.motorcycle * pollutantConfig.totalDisplayScale,
        };

        const baseTotal = baseBreakdown.car + baseBreakdown.truck + baseBreakdown.bus + baseBreakdown.motorcycle;
        const total = baseTotal * congestion.alpha;
        const totalDisplay = total * pollutantConfig.totalDisplayScale;
        const totalVehicles = counts.car + counts.truck + counts.bus + counts.motorcycle;
        const intensity = total / Math.max(totalVehicles, 1);
        const denominator = Math.max(totalVehicles, 1);
        const intensityBreakdown = {
            car: adjustedBreakdown.car / denominator,
            truck: adjustedBreakdown.truck / denominator,
            bus: adjustedBreakdown.bus / denominator,
            motorcycle: adjustedBreakdown.motorcycle / denominator,
        };
        const heatWeight = metric === 'intensity' ? intensity : total;

        return {
            ...camera,
            heat_weight: heatWeight,
            total_unique_vehicles: Math.max(toSafeNumber(camera.total_unique_vehicles, 0), totalVehicles),
            emissions: {
                pollutant: pollutantConfig.pollutant,
                pollutantLabel: pollutantConfig.label,
                metric,
                z: congestion.z,
                alpha: congestion.alpha,
                p10: congestionCache.p10,
                p90: congestionCache.p90,
                total,
                totalDisplay,
                intensity,
                totalVehicles,
                breakdownTotal: adjustedBreakdown,
                breakdownTotalDisplay: adjustedBreakdownDisplay,
                breakdownIntensity: intensityBreakdown,
                totalUnit: pollutantConfig.totalUnit,
                intensityUnit: pollutantConfig.intensityUnit,
            },
        };
    });
}

function computeTrafficWeights(cameras) {
    return cameras.map((camera) => ({
        ...camera,
        heat_weight: toSafeNumber(camera.total_unique_vehicles, 0),
        emissions: null,
    }));
}

function normalizeHeatmapWeights(cameras) {
    const weights = cameras
        .map((camera) => toSafeNumber(camera.heat_weight, 0))
        .filter((value) => Number.isFinite(value))
        .sort((a, b) => a - b);

    const p05 = weights.length > 0 ? percentileFromSorted(weights, 0.05) : 0;
    const p95 = weights.length > 0 ? percentileFromSorted(weights, 0.95) : 0;
    const spread = p95 - p05;
    const maxWeight = weights.length > 0 ? weights[weights.length - 1] : 0;
    const fallbackDenominator = maxWeight > 0 ? maxWeight : 1;

    const normalizedCameras = cameras.map((camera) => {
        const weight = toSafeNumber(camera.heat_weight, 0);
        const intensity = spread > 0
            ? clamp01((weight - p05) / spread)
            : clamp01(weight / fallbackDenominator);
        return {
            ...camera,
            heat_weight: weight,
            intensity,
            traffic_level: trafficLevelFromIntensity(intensity),
        };
    });

    return {
        cameras: normalizedCameras,
        scale: { p05, p95 },
    };
}

function buildHeatmapSummary(cameras) {
    let heavyCount = 0;
    let moderateCount = 0;
    let lightCount = 0;
    let totalVehicles = 0;
    let totalEmissionsDisplay = 0;
    let totalEmissionsRaw = 0;
    let weightedVehicleCount = 0;

    cameras.forEach((camera) => {
        totalVehicles += toSafeNumber(camera.total_unique_vehicles, 0);

        if (camera.traffic_level === 'heavy') {
            heavyCount += 1;
        } else if (camera.traffic_level === 'moderate') {
            moderateCount += 1;
        } else {
            lightCount += 1;
        }

        if (camera.emissions) {
            totalEmissionsDisplay += toSafeNumber(camera.emissions.totalDisplay, 0);
            totalEmissionsRaw += toSafeNumber(camera.emissions.total, 0);
            weightedVehicleCount += toSafeNumber(camera.emissions.totalVehicles, 0);
        }
    });

    if (heatmapMode === 'emissions') {
        const pollutantMeta = resolvePollutantConfig(selectedPollutant, HEATMAP_POLLUTANTS);
        const usingIntensity = selectedEmissionMetric === 'intensity';
        const metricValue = usingIntensity
            ? (totalEmissionsRaw / Math.max(weightedVehicleCount, 1))
            : totalEmissionsDisplay;

        return {
            camera_count: cameras.length,
            heavy_count: heavyCount,
            moderate_count: moderateCount,
            light_count: lightCount,
            metricValue,
            metricIsFloat: true,
            heavyLabel: 'High Emission Cameras',
            metricLabel: usingIntensity
                ? `${pollutantMeta.label} Intensity (${pollutantMeta.intensityUnit})`
                : `Total ${pollutantMeta.label} (${pollutantMeta.totalUnit})`,
            metricDescription: usingIntensity
                ? 'Congestion-adjusted emissions per vehicle across all cameras'
                : 'Congestion-adjusted daily emissions across all cameras',
        };
    }

    return {
        camera_count: cameras.length,
        heavy_count: heavyCount,
        moderate_count: moderateCount,
        light_count: lightCount,
        metricValue: totalVehicles,
        metricIsFloat: false,
        heavyLabel: 'Heavy Traffic Cameras',
        metricLabel: 'Total Vehicles (Date)',
        metricDescription: 'Aggregate count from all active cameras',
    };
}

function emissionsPopupHtml(camera) {
    if (!camera.emissions) return '';

    const emissions = camera.emissions;
    const usingIntensity = emissions.metric === 'intensity';
    const unit = usingIntensity ? emissions.intensityUnit : emissions.totalUnit;
    const totalValue = usingIntensity ? emissions.intensity : emissions.totalDisplay;
    const breakdown = usingIntensity ? emissions.breakdownIntensity : emissions.breakdownTotalDisplay;
    const metricLabel = usingIntensity ? 'Per-vehicle intensity' : 'Total';

    return `
        <div class="mt-3 pt-2 border-t border-gray-200">
            <div class="text-xs font-semibold mb-1">Emissions (${emissions.pollutantLabel}, ${metricLabel})</div>
            <p><strong>Congestion alpha:</strong> ${formatMeasure(emissions.alpha, 3)} (z=${formatMeasure(emissions.z, 3)})</p>
            <p><strong>Cars:</strong> ${formatMeasure(breakdown.car, 2)} ${unit}</p>
            <p><strong>Buses:</strong> ${formatMeasure(breakdown.bus, 2)} ${unit}</p>
            <p><strong>Trucks:</strong> ${formatMeasure(breakdown.truck, 2)} ${unit}</p>
            <p><strong>Motorcycles:</strong> ${formatMeasure(breakdown.motorcycle, 2)} ${unit}</p>
            <p><strong>Total:</strong> ${formatMeasure(totalValue, 2)} ${unit}</p>
        </div>
    `;
}

function cameraPopupHtml(camera, chartCanvasId) {
    const cameraName = camera.camera_name || `Camera ${camera.camera_id}`;
    const latNum = Number(camera.latitude);
    const lngNum = Number(camera.longitude);
    const lat = Number.isFinite(latNum) ? latNum.toFixed(5) : 'n/a';
    const lng = Number.isFinite(lngNum) ? lngNum.toFixed(5) : 'n/a';
    return `
        <div class="p-2 text-sm" style="max-width:min(72vw, 280px); max-height:min(62vh, 380px); overflow-y:auto;">
            <h3 class="font-bold mb-1">${cameraName}</h3>
            <p class="text-xs text-gray-500 mb-2">ID ${camera.camera_id} | ${lat}, ${lng}</p>
            <div class="space-y-1">
                <p><strong>Total vehicles:</strong> ${formatCount(camera.total_unique_vehicles)}</p>
                <p><strong>Cars:</strong> ${formatCount(camera.car_count)}</p>
                <p><strong>Buses:</strong> ${formatCount(camera.bus_count)}</p>
                <p><strong>Trucks:</strong> ${formatCount(camera.truck_count)}</p>
                <p><strong>Motorcycles:</strong> ${formatCount(camera.motorcycle_count)}</p>
                <p><strong>Peak/frame:</strong> ${formatCount(camera.peak_vehicles_per_frame)}</p>
            </div>
            ${emissionsPopupHtml(camera)}
            <div class="mt-3">
                <div class="text-xs font-semibold mb-1">Vehicle Mix</div>
                <div style="height: 140px;">
                    <canvas id="${chartCanvasId}"></canvas>
                </div>
            </div>
        </div>
    `;
}

function renderHeatmapMarkers(cameras) {
    clearTrafficMarkers();
    if (!map) return;

    const bounds = [];
    cameras.forEach((camera) => {
        const lat = Number(camera.latitude);
        const lng = Number(camera.longitude);
        if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;

        const intensity = Number(camera.intensity || 0);
        const markerColor = colorFromIntensity(intensity);
        const baseRadius = currentDiffusionRadiusMeters();
        const circleRadius = baseRadius * (1 + intensity * 1.8);

        const circle = L.circle([lat, lng], {
            radius: circleRadius,
            color: markerColor,
            weight: 1,
            fillColor: markerColor,
            fillOpacity: 0.25,
            opacity: 0.4,
            interactive: false,
            bubblingMouseEvents: false,
        }).addTo(map);

        if (intensity > 0.7) {
            const initialRadius = circleRadius / PULSE_RADIUS_DIVISOR;
            const pulseIcon = L.divIcon({
                className: 'pulse-marker',
                html: `
                    <div class="pulse-ring"
                        style="
                            border: 2px solid ${markerColor};
                            width: ${initialRadius}px;
                            height: ${initialRadius}px;
                            border-radius: 50%;
                            position: absolute;
                            top: -${initialRadius / 2}px;
                            left: -${initialRadius / 2}px;">
                    </div>
                `,
                iconSize: [0, 0]
            });

            const pulseMarker = L.marker([lat, lng], {
                icon: pulseIcon,
                interactive: false,
                keyboard: false,
            }).addTo(map);
            pulseMarkers.push({
                marker: pulseMarker,
                circleRadius: circleRadius,
                baseZoom: map.getZoom(),
            });
            trafficMarkers.push(pulseMarker);
        }

        const chartCanvasId = `vehiclePie-${camera.camera_id}-${Math.random().toString(36).slice(2, 8)}`;
        const popupHtml = cameraPopupHtml(camera, chartCanvasId);

        const hitMarker = L.circleMarker([lat, lng], {
            radius: CAMERA_HIT_RADIUS_PX,
            color: markerColor,
            fillColor: markerColor,
            opacity: 0.001,
            fillOpacity: 0.001,
            weight: 1,
        }).addTo(map);
        hitMarker.bringToFront();

        hitMarker.bindPopup(popupHtml, {
            maxWidth: 300,
            keepInView: true,
            autoPan: true,
            autoPanPaddingTopLeft: [24, 24],
            autoPanPaddingBottomRight: [24, 24],
        });

        const onPopupOpen = () => {
            activeCameraJumpId = String(camera.camera_id ?? '');
            setTimeout(() => renderVehiclePieChart(chartCanvasId, camera), 0);
        };
        const onPopupClose = () => {
            destroyPopupChart(chartCanvasId);
        };

        hitMarker.on('popupopen', onPopupOpen);
        hitMarker.on('popupclose', onPopupClose);
        hitMarker.on('click', () => {
            activeCameraJumpId = String(camera.camera_id ?? '');
        });

        trafficMarkers.push(circle);
        trafficMarkers.push(hitMarker);
        cameraJumpPoints.push({
            cameraId: String(camera.camera_id ?? ''),
            lat,
            lng,
            marker: hitMarker,
        });
        bounds.push([lat, lng]);
    });

    if (bounds.length > 0) {
        map.fitBounds(bounds, { padding: [30, 30], maxZoom: 13 });
    }
}

function isTypingTarget(target) {
    if (!target) return false;
    const tag = String(target.tagName || '').toUpperCase();
    return (
        target.isContentEditable
        || tag === 'INPUT'
        || tag === 'TEXTAREA'
        || tag === 'SELECT'
    );
}

function isMapVisibleForKeyboardNavigation() {
    if (!map || typeof map.getContainer !== 'function') return false;
    const container = map.getContainer();
    if (!container) return false;

    const rect = container.getBoundingClientRect();
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    const visibleTop = Math.max(0, rect.top);
    const visibleBottom = Math.min(viewportHeight, rect.bottom);
    const visibleHeight = Math.max(0, visibleBottom - visibleTop);
    const minVisibleHeight = Math.min(rect.height * 0.35, 160);

    return visibleHeight >= minVisibleHeight;
}

function getCurrentJumpPoint() {
    if (!map || cameraJumpPoints.length === 0) return null;
    if (activeCameraJumpId) {
        const active = cameraJumpPoints.find((point) => point.cameraId === activeCameraJumpId);
        if (active) return active;
    }

    const center = map.getCenter();
    let bestPoint = cameraJumpPoints[0];
    let bestDistance = Number.POSITIVE_INFINITY;

    cameraJumpPoints.forEach((point) => {
        const distance = map.distance(center, L.latLng(point.lat, point.lng));
        if (distance < bestDistance) {
            bestDistance = distance;
            bestPoint = point;
        }
    });

    activeCameraJumpId = bestPoint.cameraId;
    return bestPoint;
}

function findDirectionalJumpPoint(currentPoint, directionKey) {
    if (!currentPoint || cameraJumpPoints.length <= 1) return null;

    const vectors = {
        ArrowUp: { x: 0, y: 1 },
        ArrowDown: { x: 0, y: -1 },
        ArrowLeft: { x: -1, y: 0 },
        ArrowRight: { x: 1, y: 0 },
    };
    const dir = vectors[directionKey];
    if (!dir) return null;

    const cosLat = Math.max(0.2, Math.cos((currentPoint.lat * Math.PI) / 180));
    let best = null;
    let bestScore = Number.POSITIVE_INFINITY;

    cameraJumpPoints.forEach((point) => {
        if (point.cameraId === currentPoint.cameraId) return;

        const dx = (point.lng - currentPoint.lng) * cosLat;
        const dy = point.lat - currentPoint.lat;
        const projected = (dx * dir.x) + (dy * dir.y);
        if (projected <= 0) return;

        const lateral = Math.abs((dx * dir.y) - (dy * dir.x));
        const distance = Math.hypot(dx, dy);
        const score = distance + (lateral * 2.2);

        if (score < bestScore) {
            bestScore = score;
            best = point;
        }
    });

    if (best) return best;

    // Fallback if no point exists in the requested direction.
    return cameraJumpPoints
        .filter((point) => point.cameraId !== currentPoint.cameraId)
        .sort((a, b) => {
            const da = Math.hypot((a.lng - currentPoint.lng) * cosLat, a.lat - currentPoint.lat);
            const db = Math.hypot((b.lng - currentPoint.lng) * cosLat, b.lat - currentPoint.lat);
            return da - db;
        })[0] || null;
}

function focusJumpPoint(point) {
    if (!map || !point || !point.marker) return;
    activeCameraJumpId = point.cameraId;
    map.panTo([point.lat, point.lng], { animate: true, duration: 0.35 });
    point.marker.openPopup();
}

function handleMapArrowNavigation(event) {
    if (!map) return;
    if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    if (isTypingTarget(event.target)) return;
    if (!isMapVisibleForKeyboardNavigation()) return;
    if (cameraJumpPoints.length === 0) return;

    const currentPoint = getCurrentJumpPoint();
    if (!currentPoint) return;

    const nextPoint = findDirectionalJumpPoint(currentPoint, event.key);
    if (!nextPoint) return;

    event.preventDefault();
    focusJumpPoint(nextPoint);
}

function bindMapArrowNavigation() {
    if (mapArrowNavigationBound) return;
    mapArrowNavigationBound = true;
    document.addEventListener('keydown', handleMapArrowNavigation);
}

async function fetchTrafficMapData(date = null) {
    const url = new URL(TRAFFIC_MAP_API_URL, window.location.href);
    if (date) {
        url.searchParams.set('date', date);
    }

    const response = await fetch(url.toString());
    if (!response.ok) {
        throw new Error(`Traffic map request failed with status ${response.status}`);
    }

    const data = await response.json();
    if (data.error) {
        throw new Error(data.error);
    }
    return data;
}

function renderCurrentHeatmap() {
    if (!latestTrafficPayload) return;

    const rawCameras = Array.isArray(latestTrafficPayload.cameras) ? latestTrafficPayload.cameras : [];
    const dateKey = latestTrafficPayload.selected_date || selectedTrafficDate || 'no-date';

    const weightedCameras = heatmapMode === 'emissions'
        ? computeEmissionsWeights(rawCameras, {
            pollutant: selectedPollutant,
            metric: selectedEmissionMetric,
            k: HEATMAP_CONGESTION_K,
            EF: HEATMAP_POLLUTANTS,
            dateKey,
        })
        : computeTrafficWeights(rawCameras);

    const normalizedResult = normalizeHeatmapWeights(weightedCameras);
    const normalized = normalizedResult.cameras;
    updateHeatmapLegend(normalizedResult.scale);
    renderHeatmapMarkers(normalized);

    const summary = buildHeatmapSummary(normalized);
    updateTrafficSummary(summary);

    const selectedDateText = latestTrafficPayload.selected_date || 'n/a';
    if (heatmapMode === 'emissions') {
        const metricText = selectedEmissionMetric === 'intensity' ? 'per-vehicle intensity' : 'total emissions';
        setTrafficMapStatus(
            `Showing ${formatCount(summary.camera_count)} cameras for ${selectedDateText} (${selectedPollutant} ${metricText})`
        );
    } else {
        setTrafficMapStatus(`Showing ${formatCount(summary.camera_count)} cameras for ${selectedDateText} (traffic)`);
    }
}

function updateDiffusionRadiusLabel() {
    const row = document.getElementById('diffusionLegendRow');
    const label = document.getElementById('diffusionRadiusValue');
    if (row) {
        row.classList.toggle('hidden', heatmapMode !== 'emissions');
    }
    if (!label) return;
    label.textContent = `Diffusion radius: ${formatMeasure(currentDiffusionRadiusMeters(), 0)} m (fixed)`;
}

function syncHeatmapControls() {
    const trafficBtn = document.getElementById('btn-traffic');
    const emissionsBtn = document.getElementById('btn-emissions');
    const emissionsControls = document.getElementById('emissionsControls');
    const metricTotalBtn = document.getElementById('btn-metric-total');
    const metricIntensityBtn = document.getElementById('btn-metric-intensity');
    const methodPanel = document.getElementById('methodPanel');

    if (trafficBtn) {
        trafficBtn.classList.toggle('opacity-50', heatmapMode !== 'traffic');
    }
    if (emissionsBtn) {
        emissionsBtn.classList.toggle('opacity-50', heatmapMode !== 'emissions');
    }

    if (emissionsControls) {
        emissionsControls.classList.toggle('hidden', heatmapMode !== 'emissions');
    }

    if (metricTotalBtn) {
        metricTotalBtn.classList.toggle('opacity-50', selectedEmissionMetric !== 'total');
    }
    if (metricIntensityBtn) {
        metricIntensityBtn.classList.toggle('opacity-50', selectedEmissionMetric !== 'intensity');
    }

    if (heatmapMode !== 'emissions' && methodPanel) {
        methodPanel.classList.add('hidden');
    }

    updateHeatmapLegend();
    updateDiffusionRadiusLabel();
}

function setEmissionMetric(metric) {
    const normalizedMetric = metric === 'intensity' ? 'intensity' : 'total';
    if (selectedEmissionMetric === normalizedMetric) return;
    selectedEmissionMetric = normalizedMetric;
    syncHeatmapControls();
    if (heatmapMode === 'emissions') {
        renderCurrentHeatmap();
    }
}

function initHeatmapControls() {
    const pollutantSelect = document.getElementById('pollutantSelect');
    const methodPanelToggle = document.getElementById('methodPanelToggle');
    const methodPanel = document.getElementById('methodPanel');

    if (pollutantSelect) {
        const pollutantKeys = Object.keys(HEATMAP_POLLUTANTS);
        const options = pollutantKeys.length > 0 ? pollutantKeys : ['CO2'];

        if (!options.includes(selectedPollutant)) {
            selectedPollutant = options[0];
        }

        pollutantSelect.innerHTML = options
            .map((key) => {
                const meta = resolvePollutantConfig(key, HEATMAP_POLLUTANTS);
                return `<option value="${key}">${meta.label}</option>`;
            })
            .join('');
        pollutantSelect.value = selectedPollutant;

        if (pollutantSelect.dataset.bound !== 'true') {
            pollutantSelect.dataset.bound = 'true';
            pollutantSelect.addEventListener('change', (event) => {
                selectedPollutant = event.target.value;
                if (heatmapMode === 'emissions') {
                    renderCurrentHeatmap();
                }
            });
        }
    }

    if (methodPanelToggle && methodPanel && methodPanelToggle.dataset.bound !== 'true') {
        methodPanelToggle.dataset.bound = 'true';
        methodPanelToggle.addEventListener('click', () => {
            methodPanel.classList.toggle('hidden');
        });
    }

    updateDiffusionRadiusLabel();

    syncHeatmapControls();
}

async function loadTrafficMap(date = null) {
    try {
        setTrafficMapStatus('Loading traffic data...');
        const data = await fetchTrafficMapData(date);
        const cameras = Array.isArray(data.cameras) ? data.cameras : [];
        const dateKey = data.selected_date || selectedTrafficDate || 'no-date';
        const cacheKey = congestionCacheKey(dateKey, HEATMAP_CONGESTION_K);

        emissionsCongestionCache.delete(cacheKey);
        buildCongestionCacheForDate(dateKey, cameras, HEATMAP_CONGESTION_K);

        latestTrafficPayload = data;
        updateHeroStatsFromTrafficPayload(data);
        updateTrafficDateControls(data.selected_date, data.available_dates || []);
        renderCurrentHeatmap();
    } catch (error) {
        console.error('Failed to load traffic map data', error);
        latestTrafficPayload = null;
        updateTrafficDateControls(null, []);
        updateTrafficSummary({
            camera_count: 0,
            heavy_count: 0,
            metricValue: 0,
            metricIsFloat: false,
            heavyLabel: 'Heavy Traffic Cameras',
            metricLabel: 'Total Vehicles (Date)',
            metricDescription: 'Aggregate count from all active cameras',
        });
        clearTrafficMarkers();
        setTrafficMapStatus(`Unable to load traffic data: ${error.message}`, true);
    }
}

function initMap() {
    const chicagoBounds = L.latLngBounds(
        [41.60, -88.05],
        [42.08, -87.50]
    );

    map = L.map('map', {
        minZoom: 10,
        maxZoom: 18,
        maxBounds: chicagoBounds,
        maxBoundsViscosity: 1.0
    }).setView([41.8781, -87.6298], 12);
    
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
        subdomains: 'abcd',
        maxZoom: 19,
        minZoom: 10
    }).addTo(map);

    map.fitBounds(chicagoBounds);
    map.on('zoom', updatePulseMarkers);
    bindMapArrowNavigation();
    initHeatmapControls();
    loadTrafficMap();
}

function toggleLayer(type) {
    const nextMode = type === 'emissions' ? 'emissions' : 'traffic';
    if (heatmapMode !== nextMode) {
        heatmapMode = nextMode;
        syncHeatmapControls();
        if (latestTrafficPayload) {
            renderCurrentHeatmap();
        }
    }
}

// Camera and Video Upload
let stream = null;
let analysisInterval = null;

async function startCamera() {
    try {
        stream = await navigator.mediaDevices.getUserMedia({ 
            video: { facingMode: "environment" } 
        });
        const video = document.getElementById('liveVideo');
        video.srcObject = stream;
        video.classList.remove('hidden');
        document.getElementById('cameraPreview').querySelector('.text-center').classList.add('hidden');
        
        // Start simulated analysis
        startAnalysis();
    } catch (err) {
        alert('Camera access denied or not available');
    }
}

function stopCamera() {
    if (stream) {
        stream.getTracks().forEach(track => track.stop());
        stream = null;
    }
    document.getElementById('liveVideo').classList.add('hidden');
    document.getElementById('cameraPreview').querySelector('.text-center').classList.remove('hidden');
    stopAnalysis();
}

function captureFrame() {
    const video = document.getElementById('liveVideo');
    const canvas = document.getElementById('analysisCanvas');
    const ctx = canvas.getContext('2d');
    
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    ctx.drawImage(video, 0, 0);
    
    canvas.classList.remove('hidden');
    setTimeout(() => canvas.classList.add('hidden'), 1000);
    
    // Simulate detection
    simulateDetection();
}

function startAnalysis() {
    document.getElementById('analysisResults').classList.add('hidden');
    document.getElementById('activeAnalysis').classList.remove('hidden');
    
    analysisInterval = setInterval(() => {
        simulateDetection();
    }, 2000);
}

function stopAnalysis() {
    if (analysisInterval) {
        clearInterval(analysisInterval);
        analysisInterval = null;
    }
    document.getElementById('analysisResults').classList.remove('hidden');
    document.getElementById('activeAnalysis').classList.add('hidden');
}

function simulateDetection() {
    // Simulate AI detection results
    const vehicles = Math.floor(Math.random() * 15) + 5;
    const trucks = Math.floor(vehicles * 0.3);
    const emissions = (vehicles * 2.5 + trucks * 8).toFixed(1);
    
    document.getElementById('vehicleCount').textContent = vehicles;
    document.getElementById('truckDetected').textContent = trucks;
    document.getElementById('liveEmissions').textContent = `${emissions} kg/h`;
    
    function updateUI() {
        if (userLocation) {
            const latDir = userLocation.lat >= 0 ? 'N' : 'S';
            const lngDir = userLocation.lng >= 0 ? 'E' : 'W';

            document.getElementById('gpsCoords').textContent =
                `${Math.abs(userLocation.lat).toFixed(5)}°${latDir}, 
                ${Math.abs(userLocation.lng).toFixed(5)}°${lngDir}`;
        } else {
            document.getElementById('gpsCoords').textContent = "Location unavailable";
        }

        document.getElementById('timestamp').textContent = new Date().toLocaleTimeString();
    }

    fetchUserLocation(updateUI);
}

function fetchUserLocation(callback) {
    if (!navigator.geolocation) {
        console.warn("Geolocation not supported.");
        return;
    }

    navigator.geolocation.getCurrentPosition(
        (position) => {
            userLocation = {
                lat: position.coords.latitude,
                lng: position.coords.longitude
            };

            if (callback) callback();
        },
        (error) => {
            console.error("Location error:", error);
        },
        {
            enableHighAccuracy: true,
            timeout: 10000,
            maximumAge: 0
        }
    );
}

function handleVideoUpload(event) {
    const file = event.target.files[0];
    if (!file) return;
    
    const progressDiv = document.getElementById('uploadProgress');
    const progressBar = document.getElementById('progressBar');
    const status = document.getElementById('uploadStatus');
    
    progressDiv.classList.remove('hidden');
    
    // Simulate upload progress
    let progress = 0;
    const interval = setInterval(() => {
        progress += Math.random() * 15;
        if (progress >= 100) {
            progress = 100;
            clearInterval(interval);
            status.textContent = 'Processing video with AI model...';
            
            setTimeout(() => {
                status.textContent = 'Analysis complete! Data added to dataset.';
                progressDiv.classList.add('hidden');
                document.getElementById('analysisResults').classList.add('hidden');
                document.getElementById('activeAnalysis').classList.remove('hidden');
                simulateDetection();
            }, 2000);
        }
        progressBar.style.width = `${progress}%`;
    }, 200);
}

// Chat functionality
async function sendMessage() {
    const input = document.getElementById('chatInput');
    if (!input) return;

    const message = input.value.trim();
    if (!message) return;
    
    addMessage(message, 'user');
    input.value = '';
    
    // Show typing indicator
    showTyping();
    
    try {
        const response = await fetchRagResponse(message);
        removeTyping();
        addMessage(response.answer || "I don't have enough context to answer that.", 'bot');
        if (typeof response.retrieved_chunks !== 'undefined') {
            addMessage(`Retrieved chunks: ${response.retrieved_chunks}`, 'bot');
        }

        if (response.citations && response.citations.length > 0) {
            const refs = response.citations
                .slice(0, 3)
                .map((c) => c.source_url || `${c.doc_id || 'doc'}:${c.chunk_id || 'chunk'}`);
            addMessage(`Sources: ${refs.join(' | ')}`, 'bot');
        }
    } catch (err) {
        removeTyping();
        if (CHAT_TEST_MODE) {
            addMessage(`Backend error: ${err.message}`, 'bot');
            return;
        }
        const fallback = generateAIResponse(message);
        addMessage(fallback, 'bot');
    }
}

function quickAsk(question) {
    document.getElementById('chatInput').value = question;
    sendMessage();
}

function addMessage(text, sender) {
    const container = document.getElementById('chatContainer');
    const div = document.createElement('div');
    div.className = 'chat-message flex gap-3 ' + (sender === 'user' ? 'flex-row-reverse' : '');
    
    const avatar = sender === 'user' 
        ? '<div class="w-8 h-8 rounded-full bg-gray-600 flex-shrink-0 flex items-center justify-center"><i class="fas fa-user text-white text-xs"></i></div>'
        : '<div class="w-8 h-8 rounded-full bg-gradient-to-r from-purple-500 to-pink-500 flex-shrink-0 flex items-center justify-center"><i class="fas fa-robot text-white text-xs"></i></div>';
    
    const bubble = sender === 'user'
        ? '<div class="glass-panel rounded-2xl rounded-tr-none px-4 py-3 max-w-[80%] bg-blue-600/20 border-blue-500/30">' + text + '</div>'
        : '<div class="glass-panel rounded-2xl rounded-tl-none px-4 py-3 max-w-[80%]">' + text + '</div>';
    
    div.innerHTML = avatar + bubble;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function showTyping() {
    const container = document.getElementById('chatContainer');
    const div = document.createElement('div');
    div.id = 'typingIndicator';
    div.className = 'chat-message flex gap-3';
    div.innerHTML = `
        <div class="w-8 h-8 rounded-full bg-gradient-to-r from-purple-500 to-pink-500 flex-shrink-0 flex items-center justify-center">
            <i class="fas fa-robot text-white text-xs"></i>
        </div>
        <div class="glass-panel rounded-2xl rounded-tl-none px-4 py-3">
            <div class="typing-indicator">
                <span></span><span></span><span></span>
            </div>
        </div>
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function removeTyping() {
    const indicator = document.getElementById('typingIndicator');
    if (indicator) indicator.remove();
}

async function fetchRagResponse(question) {
    const res = await fetch(CHAT_API_URL, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ question })
    });
    if (!res.ok) {
        throw new Error(`Chat request failed with status ${res.status}`);
    }
    const data = await res.json();
    if (data.error) {
        throw new Error(data.error);
    }
    return data;
}

function generateAIResponse(input) {
    const lower = input.toLowerCase();
    
    if (lower.includes('current') || lower.includes('level')) {
        return "Current emission levels are moderate at 145 ppm across the monitored area. Downtown shows the highest concentration at 210 ppm due to heavy traffic congestion. I recommend avoiding the central business district for the next 2 hours.";
    }
    if (lower.includes('rush') || lower.includes('hour') || lower.includes('peak')) {
        return "Based on historical data and current traffic patterns, we expect peak emissions between 17:30-19:00 today. The I-90/I-94 corridor will likely exceed 300 ppm during this period. Alternative routes through residential zones show 40% lower emission levels.";
    }
    if (lower.includes('worst') || lower.includes('polluted') || lower.includes('area')) {
        return "The top 3 most polluted zones right now are: 1) Industrial District (245 ppm) - factory emissions, 2) Downtown Core (210 ppm) - traffic congestion, 3) Port Harbor (195 ppm) - shipping activity. These areas exceed WHO recommended limits by 2-3x.";
    }
    if (lower.includes('reduce') || lower.includes('footprint') || lower.includes('help')) {
        return "Here are personalized recommendations: 1) Use public transit during peak hours (7-9 AM, 5-7 PM) to reduce exposure by 60%, 2) Consider cycling routes through parks - our data shows 80% lower pollution levels, 3) If driving is necessary, use recirculated air mode in high-traffic zones, 4) Contribute footage via our upload feature to help map cleaner routes.";
    }
    if (lower.includes('truck') || lower.includes('heavy')) {
        return "Heavy vehicles contribute disproportionately to emissions - approximately 40% of total pollution despite being only 10% of traffic. Our regression model estimates each truck produces 8x the emissions of a passenger car. Current truck density is highest on the I-90/I-94 corridor (892 vehicles/hour).";
    }
    if (lower.includes('prediction') || lower.includes('tomorrow') || lower.includes('forecast')) {
        return "Tomorrow's forecast shows similar patterns with a high probability of elevated emissions during morning rush (7-9 AM). Weather conditions suggest moderate dispersion, so levels may remain 15-20% higher than today. I recommend planning outdoor activities after 10 AM.";
    }
    
    return "I can help you analyze emission trends, predict pollution levels, identify high-traffic zones, or suggest cleaner routes. I can also process your uploaded videos to detect vehicle types and estimate their environmental impact. What specific data would you like to explore?";
}

function setupDropZoneHandlers() {
    const dropZone = document.getElementById('dropZone');
    if (!dropZone) return;

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('border-green-400', 'bg-green-400/10');
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('border-green-400', 'bg-green-400/10');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('border-green-400', 'bg-green-400/10');
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            document.getElementById('videoInput').files = files;
            handleVideoUpload({ target: { files: files } });
        }
    });
}

// Initialize
window.onload = async function() {
    requireAuthForUploadRoute();

    sectionsReadyPromise = injectSectionsOnHomePage();
    await sectionsReadyPromise;
    initUserMenu();
    observeVisibleSections();

    if (document.getElementById('map')) {
        initMap();
    }

    if (document.getElementById('predictionChart') && typeof initChart === 'function' && typeof updatePrediction === 'function') {
        initChart();
        updatePrediction(12);
    }

    const requestedSection = getRequestedSection();
    if (requestedSection) {
        await scrollToSection(requestedSection, { behavior: 'auto', persist: true });
    }

    const selectedLang = document.getElementById('languageSelect')?.value || 'en';
    applyTranslations(selectedLang);
    setupDropZoneHandlers();

    if (!document.getElementById('map') && document.getElementById('vehiclesYesterday')) {
        try {
            const data = await fetchTrafficMapData();
            updateHeroStatsFromTrafficPayload(data);
        } catch (error) {
            console.error('Failed to load hero stats data', error);
        }
    }
};

function requireAuthForUploadRoute() {
    // Normalize path (no trailing slash)
    const path = window.location.pathname.replace(/\/$/, '');

    const isUploadRoute = path === '/upload' || path.startsWith('/upload/');
    if (!isUploadRoute) return;

    const currentUser = window.Auth ? window.Auth.getSessionUser() : null;
    if (currentUser) return;

    // Save full path + query + hash so we can return after login
    const returnTo = window.location.pathname + window.location.search + window.location.hash;
    sessionStorage.setItem('redirectAfterLogin', returnTo);

    // Redirect to login (preserve your current login path convention)
    window.location.href = '/login/';
}
