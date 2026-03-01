function createNavigation() {
    const isHomePage = window.location.pathname === '/' || window.location.pathname.endsWith('/index.html');
    const isSubPage = !isHomePage;
    const dashboardLink = isHomePage ? "javascript:void(0)" : "../?section=top";
    const emissionsLink = isHomePage ? "javascript:void(0)" : "../?section=heatmap";
    const uploadLink = "../upload/";
    const chatLink = isHomePage ? "javascript:void(0)" : "../?section=chat";
    const profileLink = isSubPage ? "../profile/" : "profile/";
    const loginLink = isSubPage ? "../login/" : "login/";
    const currentUser = window.Auth ? window.Auth.getSessionUser() : null;
    const initials = currentUser ? window.Auth.getInitials(currentUser.name, currentUser.email) : "";
    const avatarHtml = currentUser && currentUser.avatarUrl
        ? `<img src="${currentUser.avatarUrl}" alt="Profile" class="w-8 h-8 rounded-full object-cover border border-green-300">`
        : `<span class="w-8 h-8 rounded-full bg-green-300 flex items-center justify-center text-xs font-bold">${initials}</span>`;
    const uploadAction = isHomePage
        ? 'onclick="scrollToSection(\'upload\')" href="javascript:void(0)"'
        : `href="${uploadLink}"`;

    const authControls = currentUser
        ? `
            <div class="relative" id="userMenuContainer">
                <button id="userMenuTrigger" type="button" class="flex items-center gap-2 px-2 py-1 rounded-lg border border-green-400/30 bg-blue-200/50 hover:bg-blue-300/70 transition-all">
                    ${avatarHtml}
                    <span class="hidden sm:inline text-sm max-w-28 truncate">${currentUser.name || currentUser.email}</span>
                    <i class="fas fa-chevron-down text-xs"></i>
                </button>
                <div id="userMenuDropdown" class="hidden absolute right-0 mt-2 w-44 rounded-xl glass-panel border border-gray-700 shadow-lg p-2 z-50">
                    <a href="${profileLink}" class="block px-3 py-2 rounded-lg text-sm hover:bg-gray-200">
                        <i class="fas fa-user-gear mr-2"></i>Profile
                    </a>
                    <a ${uploadAction} class="block px-3 py-2 rounded-lg text-sm hover:bg-gray-200">
                        <i class="fas fa-upload mr-2"></i>Upload
                    </a>
                    <button id="logoutBtn" type="button" class="w-full text-left px-3 py-2 rounded-lg text-sm hover:bg-gray-200 text-red-500">
                        <i class="fas fa-right-from-bracket mr-2"></i>Log Out
                    </button>
                </div>
            </div>
        `
        : `<a href="${loginLink}" class="px-3 py-1 rounded-lg bg-green-300 font-semibold hover:shadow-lg transition-all" data-i18n="nav.login">Login</a>`;

    return `
    <nav class="fixed w-full z-50 glass-panel">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex items-center justify-between h-[68px] gap-3">

                <div class="absolute left-5 top-1/2 -translate-y-1/2 flex items-center gap-4 text-sm whitespace-nowrap">
                    <img onclick="window.location.href='${isHomePage ? './?section=top' : '../?section=top'}'" src="${isHomePage ? 'image/favicon.png' : '../image/favicon.png'}" alt="VanData Logo" class="w-[84px] h-[84px] object-contain" style="cursor: pointer;">
                </div>

                <div class="absolute right-5 top-1/2 -translate-y-1/2 flex items-center gap-4 text-sm whitespace-nowrap">
                    <a href="${dashboardLink}" ${isHomePage ? 'onclick="scrollToSection(\'top\')"' : ''} class="hover:text-green-400 px-2 sm:px-3 py-2 font-medium" data-i18n="nav.dashboard">Dashboard</a>
                    <a href="${emissionsLink}" ${isHomePage ? 'onclick="scrollToSection(\'heatmap\')"' : ''} class="hover:text-green-400 px-2 sm:px-3 py-2 font-medium" data-i18n="nav.heatmap">Heat Map</a>
                    <a href="${chatLink}" ${isHomePage ? 'onclick="scrollToSection(\'chat\')"' : ''} class="hover:text-green-400 px-2 sm:px-3 py-2 font-medium" data-i18n="nav.chat">AI Assistant</a>

                    <!-- Language Selector inside nav -->
                    <select id="languageSelect"
                        class="glass-panel px-3 py-1 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-green-400 cursor-pointer">
                        <option value="en">English</option>
                        <option value="es">Español</option>
                        <option value="zh">中文</option>
                    </select>
                    ${authControls}
                </div>

            </div>
        </div>
    </nav>`;
}

function createFooter() {
    const isHomePage = window.location.pathname === '/' || window.location.pathname.endsWith('/index.html');
    const basePath = isHomePage ? '' : '../';
    
    return `
    <footer class="glass-panel mt-20">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
            
            <!-- Single Row Layout -->
            <div class="flex flex-wrap items-center justify-between gap-6">
                
                <!-- Logo -->
                <div class="flex items-center gap-3">
                    <img src="${basePath}image/favicon.png" alt="VanData" class="w-8 h-8 object-contain">
                    <div>
                        <span class="font-bold bg-gradient-to-r from-green-400 to-blue-400 bg-clip-text text-transparent">VanData</span>
                        <span class="text-[10px] text-gray-500 ml-2">Pollution Intelligence</span>
                    </div>
                </div>

                <!-- Team Pills -->
                <div class="flex flex-wrap gap-2 text-[11px]">
                    <span class="px-2 py-1 rounded-full bg-green-400/10 text-green-400/80 border border-green-400/20">Emiliano Escutia</span>
                    <span class="px-2 py-1 rounded-full bg-blue-400/10 text-blue-400/80 border border-blue-400/20">Angel Moreno</span>
                    <span class="px-2 py-1 rounded-full bg-purple-400/10 text-purple-400/80 border border-purple-400/20">Howard Su</span>
                    <span class="px-2 py-1 rounded-full bg-yellow-400/10 text-yellow-400/80 border border-yellow-400/20">Jon Hogg</span>
                </div>

                <!-- Links -->
                <div class="flex gap-4 text-xs text-gray-400">
                    <a href="${isHomePage ? 'javascript:void(0)' : basePath}" onclick="${isHomePage ? 'scrollToSection(\'top\')' : ''}" class="hover:text-green-400 transition-colors">Dashboard</a>
                    <a href="${isHomePage ? 'javascript:void(0)' : basePath + '?section=heatmap'}" onclick="${isHomePage ? 'scrollToSection(\'heatmap\')' : ''}" class="hover:text-green-400 transition-colors">Map</a>
                    <a href="${basePath}upload/" class="hover:text-green-400 transition-colors">Upload</a>
                </div>
            </div>

            <!-- Copyright Bar -->
            <div class="mt-4 pt-3 border-t border-gray-700/30 flex justify-between items-center text-[10px] text-gray-600">
                <span>&copy;2026 HackIllinois VanData</span>
                <span class="flex items-center gap-1.5">
                    <span class="w-1 h-1 rounded-full bg-green-400"></span>
                    Operational
                </span>
            </div>
        </div>
    </footer>`;
}