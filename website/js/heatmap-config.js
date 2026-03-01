(function initHeatmapConfig(global) {
    global.HEATMAP_CONFIG = Object.freeze({
        // Placeholder factors (grams per vehicle per day). Replace with calibrated values later.
        POLLUTANTS: {
            CO2: {
                label: 'CO2',
                totalUnit: 'kg/day',
                intensityUnit: 'g/vehicle',
                totalDisplayScale: 0.001,
                emissionFactors: {
                    car: 251,
                    truck: 1180,
                    bus: 950,
                    motorcycle: 110,
                },
            },
            NOx: {
                label: 'NOx',
                totalUnit: 'kg/day',
                intensityUnit: 'g/vehicle',
                totalDisplayScale: 0.001,
                emissionFactors: {
                    car: 1.1,
                    truck: 8.5,
                    bus: 7.2,
                    motorcycle: 0.4,
                },
            },
            'PM2.5': {
                label: 'PM2.5',
                totalUnit: 'kg/day',
                intensityUnit: 'g/vehicle',
                totalDisplayScale: 0.001,
                emissionFactors: {
                    car: 0.03,
                    truck: 0.35,
                    bus: 0.22,
                    motorcycle: 0.02,
                },
            },
        },
        DEFAULT_POLLUTANT: 'CO2',
        DEFAULT_METRIC: 'total',
        CONGESTION_MULTIPLIER_K: 0.5,
        COLOR_PALETTES: {
            traffic: {
                low: '#00ff88',
                medium: '#ffaa00',
                high: '#ff3366',
            },
            emissions: {
                low: '#39c5ff',
                medium: '#8b5cf6',
                high: '#ff4fd8',
            },
        },
        DIFFUSION_RADIUS_METERS: {
            traffic: 300,
            emissions: 1000,
        },
    });
})(window);
