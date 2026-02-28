// Language Switcher
document.getElementById('languageSelect').addEventListener('change', (e) => {
    const lang = e.target.value;
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (translations[lang] && translations[lang][key]) {
            el.textContent = translations[lang][key];
        }
    });
});

// Initialize Map
let map, heatLayer, markers = [];

function initMap() {
    map = L.map('map').setView([40.7128, -74.0060], 12);
    
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
        subdomains: 'abcd',
        maxZoom: 19
    }).addTo(map);
    
    // Add simulated heat map data points
    const heatPoints = [
        [40.7128, -74.0060, 0.9], // Downtown - high
        [40.7580, -73.9855, 0.8], // Midtown - high
        [40.6782, -73.9442, 0.6], // Brooklyn - medium
        [40.7282, -73.7949, 0.4], // Queens - low
        [40.8176, -73.9782, 0.7], // Industrial - high
        [40.7489, -74.0324, 0.5], // Harbor - medium
        [40.6892, -74.0445, 0.3], // Residential - low
        [40.7614, -73.9776, 0.85], // Commercial - high
    ];
    
    // Create custom markers with pulsing effect for critical areas
    heatPoints.forEach((point, idx) => {
        const intensity = point[2];
        let color = intensity > 0.7 ? '#ff3366' : intensity > 0.4 ? '#ffaa00' : '#00ff88';
        
        const marker = L.circleMarker([point[0], point[1]], {
            radius: 20 + (intensity * 30),
            fillColor: color,
            color: color,
            weight: 2,
            opacity: 0.3,
            fillOpacity: 0.2
        }).addTo(map);
        
        // Add pulsing effect for high intensity
        if (intensity > 0.7) {
            const pulseIcon = L.divIcon({
                className: 'pulse-marker',
                html: `<div class="pulse-ring" style="border: 2px solid ${color}; width: 40px; height: 40px; border-radius: 50%; position: absolute; top: -20px; left: -20px;"></div>`,
                iconSize: [0, 0]
            });
            L.marker([point[0], point[1]], {icon: pulseIcon}).addTo(map);
        }
        
        marker.bindPopup(`
            <div class="p-2">
                <h3 class="font-bold mb-1">Zone ${idx + 1}</h3>
                <p class="text-sm">Emissions: ${(intensity * 200).toFixed(0)} ppm</p>
                <p class="text-sm">Traffic: ${intensity > 0.7 ? 'Heavy' : intensity > 0.4 ? 'Moderate' : 'Light'}</p>
                <button onclick="zoomToZone(${point[0]}, ${point[1]})" class="mt-2 px-3 py-1 bg-blue-500 text-white rounded text-xs">Analyze</button>
            </div>
        `);
        
        markers.push(marker);
    });
}

function zoomToZone(lat, lng) {
    map.setView([lat, lng], 16);
    updateLocationFromCoords(lat, lng);
}

function toggleLayer(type) {
    const btn = document.getElementById(`btn-${type}`);
    btn.classList.toggle('opacity-50');
    // In real implementation, this would toggle map layers
}

// Chart.js for Predictions
let predictionChart;

function initChart() {
    const ctx = document.getElementById('predictionChart').getContext('2d');
    predictionChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: Array.from({length: 24}, (_, i) => `${i}:00`),
            datasets: [{
                label: 'Current Trend',
                data: generateHourlyData(),
                borderColor: '#00ff88',
                backgroundColor: 'rgba(0, 255, 136, 0.1)',
                tension: 0.4,
                fill: true
            }, {
                label: 'AI Prediction',
                data: generatePredictedData(),
                borderColor: '#00d4ff',
                backgroundColor: 'rgba(0, 212, 255, 0.1)',
                borderDash: [5, 5],
                tension: 0.4,
                fill: true
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    labels: { color: '#e2e8f0' }
                }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: 'rgba(255, 255, 255, 0.1)' },
                    ticks: { color: '#94a3b8' }
                },
                x: {
                    grid: { color: 'rgba(255, 255, 255, 0.1)' },
                    ticks: { color: '#94a3b8' }
                }
            }
        }
    });
}

function generateHourlyData() {
    return Array.from({length: 24}, (_, i) => {
        // Simulate rush hours at 8am and 6pm
        const base = 50;
        const morning = i >= 7 && i <= 9 ? 100 : 0;
        const evening = i >= 17 && i <= 19 ? 120 : 0;
        const random = Math.random() * 20;
        return base + morning + evening + random;
    });
}

function generatePredictedData() {
    return generateHourlyData().map((v, i) => {
        if (i < 12) return null; // Only predict future
        return v * (1 + Math.random() * 0.3);
    });
}

function updatePrediction(hour) {
    document.getElementById('selectedTime').textContent = `${hour}:00`;
    const current = Math.floor(Math.random() * 50 + 100);
    const predicted = Math.floor(current * (1.2 + Math.random() * 0.4));
    const change = Math.floor(((predicted - current) / current) * 100);
    
    document.getElementById('currentVal').textContent = `${current} ppm`;
    document.getElementById('predictedVal').textContent = `${predicted} ppm`;
    document.getElementById('changeVal').textContent = `+${change}%`;
    document.getElementById('changeVal').className = `text-xl font-bold ${change > 20 ? 'text-red-400' : 'text-yellow-400'}`;
    
    // Update chart highlight
    predictionChart.data.datasets[0].pointBackgroundColor = Array(24).fill('transparent');
    predictionChart.data.datasets[0].pointBackgroundColor[hour] = '#ffffff';
    predictionChart.data.datasets[0].pointRadius = Array(24).fill(3);
    predictionChart.data.datasets[0].pointRadius[hour] = 8;
    predictionChart.update();
}

function updateLocationTrend() {
    const location = document.getElementById('locationSelect').value;
    // Simulate different data for different locations
    const multiplier = {
        'downtown': 1.5,
        'highway': 1.2,
        'industrial': 1.8,
        'residential': 0.6,
        'harbor': 1.3
    }[location] || 1;
    
    const newData = generateHourlyData().map(v => v * multiplier);
    predictionChart.data.datasets[0].data = newData;
    predictionChart.data.datasets[1].data = newData.map((v, i) => i < 12 ? null : v * 1.2);
    predictionChart.update();
    
    // Update insight
    const insights = {
        'downtown': "Peak emissions expected at 18:00 due to rush hour traffic. Recommend alternative routing.",
        'highway': "Consistent high emissions throughout the day. Truck traffic is the primary contributor.",
        'industrial': "Early morning peak at 06:00 due to factory operations. Evening levels remain elevated.",
        'residential': "Low baseline emissions with minor peaks during school drop-off times.",
        'harbor': "Shipping activity creates variable patterns. Night shift operations increase 22:00-04:00."
    };
    document.getElementById('aiInsight').textContent = insights[location];
}

function runPrediction() {
    const btn = event.target;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>Processing...';
    setTimeout(() => {
        btn.innerHTML = '<i class="fas fa-check mr-2"></i>Updated';
        setTimeout(() => {
            btn.innerHTML = '<i class="fas fa-calculator mr-2"></i>Calculate Prediction';
        }, 2000);
    }, 1500);
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
    
    // Update GPS (simulated)
    const lat = (40.7128 + (Math.random() - 0.5) * 0.1).toFixed(4);
    const lng = (-74.0060 + (Math.random() - 0.5) * 0.1).toFixed(4);
    document.getElementById('gpsCoords').textContent = `${lat}°N, ${lng}°W`;
    
    document.getElementById('timestamp').textContent = new Date().toLocaleTimeString();
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
function sendMessage() {
    const input = document.getElementById('chatInput');
    const message = input.value.trim();
    if (!message) return;
    
    addMessage(message, 'user');
    input.value = '';
    
    // Show typing indicator
    showTyping();
    
    // Simulate AI response
    setTimeout(() => {
        removeTyping();
        const response = generateAIResponse(message);
        addMessage(response, 'bot');
    }, 1500 + Math.random() * 1000);
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

function generateAIResponse(input) {
    const lower = input.toLowerCase();
    
    if (lower.includes('current') || lower.includes('level')) {
        return "Current emission levels are moderate at 145 ppm across the monitored area. Downtown shows the highest concentration at 210 ppm due to heavy traffic congestion. I recommend avoiding the central business district for the next 2 hours.";
    }
    if (lower.includes('rush') || lower.includes('hour') || lower.includes('peak')) {
        return "Based on historical data and current traffic patterns, we expect peak emissions between 17:30-19:00 today. The Highway I-95 corridor will likely exceed 300 ppm during this period. Alternative routes through residential zones show 40% lower emission levels.";
    }
    if (lower.includes('worst') || lower.includes('polluted') || lower.includes('area')) {
        return "The top 3 most polluted zones right now are: 1) Industrial District (245 ppm) - factory emissions, 2) Downtown Core (210 ppm) - traffic congestion, 3) Port Harbor (195 ppm) - shipping activity. These areas exceed WHO recommended limits by 2-3x.";
    }
    if (lower.includes('reduce') || lower.includes('footprint') || lower.includes('help')) {
        return "Here are personalized recommendations: 1) Use public transit during peak hours (7-9 AM, 5-7 PM) to reduce exposure by 60%, 2) Consider cycling routes through parks - our data shows 80% lower pollution levels, 3) If driving is necessary, use recirculated air mode in high-traffic zones, 4) Contribute footage via our upload feature to help map cleaner routes.";
    }
    if (lower.includes('truck') || lower.includes('heavy')) {
        return "Heavy vehicles contribute disproportionately to emissions - approximately 40% of total pollution despite being only 10% of traffic. Our regression model estimates each truck produces 8x the emissions of a passenger car. Current truck density is highest on Highway I-95 (892 vehicles/hour).";
    }
    if (lower.includes('prediction') || lower.includes('tomorrow') || lower.includes('forecast')) {
        return "Tomorrow's forecast shows similar patterns with a high probability of elevated emissions during morning rush (7-9 AM). Weather conditions suggest moderate dispersion, so levels may remain 15-20% higher than today. I recommend planning outdoor activities after 10 AM.";
    }
    
    return "I can help you analyze emission trends, predict pollution levels, identify high-traffic zones, or suggest cleaner routes. I can also process your uploaded videos to detect vehicle types and estimate their environmental impact. What specific data would you like to explore?";
}

function scrollToSection(id) {
    document.getElementById(id).scrollIntoView({ behavior: 'smooth' });
}

// Initialize
window.onload = function() {
    initMap();
    initChart();
    updatePrediction(12);
    
    // Simulate live stats updates
    setInterval(() => {
        document.getElementById('activeSensors').textContent = Math.floor(1200 + Math.random() * 100);
        document.getElementById('co2Level').textContent = (2.3 + Math.random() * 0.3).toFixed(1) + 'k';
        document.getElementById('truckCount').textContent = Math.floor(850 + Math.random() * 100);
    }, 5000);
};

// Drag and drop for video
const dropZone = document.getElementById('dropZone');

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