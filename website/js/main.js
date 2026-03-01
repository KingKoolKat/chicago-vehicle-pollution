document.body.insertAdjacentHTML('afterbegin', createNavigation());
const CHAT_API_URL = window.CHAT_API_URL || '/chat';
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
    if (!isHomePage) {
        window.location.href = `../?section=${encodeURIComponent(id)}`;
        return;
    }

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

// Initialize Map
let map;
let trafficMarkers = [];
let trafficDates = [];
let selectedTrafficDate = null;
const trafficPopupCharts = {};
let pulseMarkers = [];
const PULSE_RADIUS_DIVISOR = 48;

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

function colorFromIntensity(intensity) {
    const clamped = Math.max(0, Math.min(1, Number(intensity) || 0));
    if (clamped > 0.7) return '#ff3366';
    if (clamped > 0.4) return '#ffaa00';
    return '#00ff88';
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
}

function updatePulseMarkers() {
    if (!map) return;
    const currentZoom = map.getZoom();

    pulseMarkers.forEach((pulse) => {
        const scale = map.getZoomScale(currentZoom, pulse.baseZoom);
        const newRadius = (pulse.circleRadius / PULSE_RADIUS_DIVISOR) * scale;
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
    if (heavyCount) heavyCount.textContent = formatCount(summary.heavy_count);
    if (totalVehicles) totalVehicles.textContent = formatCount(summary.total_unique_vehicles);
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

function cameraPopupHtml(camera, chartCanvasId) {
    const cameraName = camera.camera_name || `Camera ${camera.camera_id}`;
    const lat = Number(camera.latitude).toFixed(5);
    const lng = Number(camera.longitude).toFixed(5);
    return `
        <div class="p-2 text-sm">
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
            <div class="mt-3">
                <div class="text-xs font-semibold mb-1">Vehicle Mix</div>
                <div style="height: 180px;">
                    <canvas id="${chartCanvasId}"></canvas>
                </div>
            </div>
        </div>
    `;
}

function renderTrafficMarkers(cameras) {
    clearTrafficMarkers();
    if (!map) return;

    const bounds = [];
    cameras.forEach((camera) => {
        const lat = Number(camera.latitude);
        const lng = Number(camera.longitude);
        if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;

        const intensity = Number(camera.intensity || 0);
        const markerColor = colorFromIntensity(intensity);
        const circleRadius = 400 * (1 + intensity * 2);

        const circle = L.circle([lat, lng], {
            radius: circleRadius,
            color: markerColor,
            weight: 1,
            fillColor: markerColor,
            fillOpacity: 0.25,
            opacity: 0.4,
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

            const pulseMarker = L.marker([lat, lng], { icon: pulseIcon }).addTo(map);
            pulseMarkers.push({
                marker: pulseMarker,
                circleRadius: circleRadius,
                baseZoom: map.getZoom(),
            });
            trafficMarkers.push(pulseMarker);
        }

        const chartCanvasId = `vehiclePie-${camera.camera_id}-${Math.random().toString(36).slice(2, 8)}`;
        const popupHtml = cameraPopupHtml(camera, chartCanvasId);

        circle.bindPopup(popupHtml, { maxWidth: 360 });

        const onPopupOpen = () => {
            setTimeout(() => renderVehiclePieChart(chartCanvasId, camera), 0);
        };
        const onPopupClose = () => {
            destroyPopupChart(chartCanvasId);
        };

        circle.on('popupopen', onPopupOpen);
        circle.on('popupclose', onPopupClose);

        trafficMarkers.push(circle);
        bounds.push([lat, lng]);
    });

    if (bounds.length > 0) {
        map.fitBounds(bounds, { padding: [30, 30], maxZoom: 13 });
    }
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

async function loadTrafficMap(date = null) {
    try {
        setTrafficMapStatus('Loading traffic data...');
        const data = await fetchTrafficMapData(date);
        const summary = data.summary || {};
        const cameras = Array.isArray(data.cameras) ? data.cameras : [];

        updateTrafficDateControls(data.selected_date, data.available_dates || []);
        updateTrafficSummary(summary);
        renderTrafficMarkers(cameras);

        const selectedDateText = data.selected_date || 'n/a';
        setTrafficMapStatus(`Showing ${formatCount(summary.camera_count)} cameras for ${selectedDateText}`);
    } catch (error) {
        console.error('Failed to load traffic map data', error);
        updateTrafficDateControls(null, []);
        updateTrafficSummary({ camera_count: 0, heavy_count: 0, total_unique_vehicles: 0 });
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
    loadTrafficMap();
}

function toggleLayer(type) {
    const btn = document.getElementById(`btn-${type}`);
    if (btn) {
        btn.classList.toggle('opacity-50');
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

        if (response.citations && response.citations.length > 0) {
            const refs = response.citations
                .slice(0, 3)
                .map((c) => c.source_url || `${c.doc_id || 'doc'}:${c.chunk_id || 'chunk'}`);
            addMessage(`Sources: ${refs.join(' | ')}`, 'bot');
        }
    } catch (err) {
        removeTyping();
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

    // Simulate live stats updates
    setInterval(() => {
        const activeSensors = document.getElementById('activeSensors');
        const co2Level = document.getElementById('co2Level');
        const truckCount = document.getElementById('truckCount');

        if (!activeSensors || !co2Level || !truckCount) return;

        activeSensors.textContent = Math.floor(1200 + Math.random() * 100);
        co2Level.textContent = (2.3 + Math.random() * 0.3).toFixed(1) + 'k';
        truckCount.textContent = Math.floor(850 + Math.random() * 100);
    }, 5000);
};
