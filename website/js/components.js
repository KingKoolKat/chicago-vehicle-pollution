function createNavigation() {
    return `
    <nav class="fixed w-full z-50 glass-panel border-b border-gray-700">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex items-center justify-between h-16">

                <div class="flex items-center space-x-3">
                    <div class="w-10 h-10 rounded-full bg-gradient-to-r from-green-400 to-blue-500 flex items-center justify-center">
                        <i class="fas fa-leaf text-white text-xl"></i>
                    </div>
                    <span class="text-2xl font-bold bg-gradient-to-r from-green-400 to-blue-400 bg-clip-text text-transparent" data-i18n="appName">
                        EcoTrack
                    </span>
                </div>

                <div class="hidden md:flex items-center space-x-8">
                    <a href="../" class="hover:text-green-400 px-3 py-2 text-sm font-medium" data-i18n="nav.dashboard">Dashboard</a>
                    <a href="../heat-map" class="hover:text-green-400 px-3 py-2 text-sm font-medium" data-i18n="nav.heatmap">Heat Map</a>
                    <a href="../upload" class="hover:text-green-400 px-3 py-2 text-sm font-medium" data-i18n="nav.upload">Upload Data</a>
                    <a href="../chatbot" class="hover:text-green-400 px-3 py-2 text-sm font-medium" data-i18n="nav.chat">AI Assistant</a>

                    <!-- Language Selector inside nav -->
                    <select id="languageSelect"
                        class="glass-panel px-3 py-1 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-green-400 cursor-pointer">
                        <option value="en">English</option>
                        <option value="es">Español</option>
                        <option value="zh">中文</option>
                    </select>
                </div>

            </div>
        </div>
    </nav>`;
}