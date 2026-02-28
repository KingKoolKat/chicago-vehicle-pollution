function createNavigation() {
    const isHomePage = window.location.pathname === '/' || window.location.pathname.endsWith('/index.html');
    const isSubPage = !isHomePage;
    const dashboardLink = isHomePage ? "javascript:void(0)" : "../?section=top";
    const emissionsLink = isHomePage ? "javascript:void(0)" : "../?section=heatmap";
    const uploadLink = isHomePage ? "upload/" : "../upload/";
    const chatLink = isHomePage ? "javascript:void(0)" : "../?section=chat";
    const loginLink = isSubPage ? "../login/" : "login/";
    const currentUser = window.Auth ? window.Auth.getSessionUser() : null;
    const initials = currentUser ? window.Auth.getInitials(currentUser.name, currentUser.email) : "";
    const uploadAction = isHomePage
        ? 'onclick="window.location.href=\'upload\'"'
        : `href="${uploadLink}"`;

    const authControls = currentUser
        ? `
            <div class="relative" id="userMenuContainer">
                <button id="userMenuTrigger" type="button" class="flex items-center gap-2 px-2 py-1 rounded-lg border border-green-400/30 bg-blue-200/50 hover:bg-blue-300/70 transition-all">
                    <span class="w-8 h-8 rounded-full bg-gradient-to-r from-green-500 to-blue-600 flex items-center justify-center text-xs font-bold">${initials}</span>
                    <span class="hidden sm:inline text-sm max-w-28 truncate">${currentUser.name || currentUser.email}</span>
                    <i class="fas fa-chevron-down text-xs"></i>
                </button>
                <div id="userMenuDropdown" class="hidden absolute right-0 mt-2 w-44 rounded-xl glass-panel border border-gray-700 shadow-lg p-2 z-50">
                    <a ${uploadAction} class="block px-3 py-2 rounded-lg text-sm hover:bg-gray-200">
                        <i class="fas fa-upload mr-2"></i>Upload
                    </a>
                    <button id="logoutBtn" type="button" class="w-full text-left px-3 py-2 rounded-lg text-sm hover:bg-gray-200 text-red-500">
                        <i class="fas fa-right-from-bracket mr-2"></i>Log Out
                    </button>
                </div>
            </div>
        `
        : `<a href="${loginLink}" class="px-3 py-1 rounded-lg bg-gradient-to-r from-green-500 to-blue-600 font-semibold hover:shadow-lg transition-all" data-i18n="nav.login">Login</a>`;

    return `
    <nav class="fixed w-full z-50 glass-panel">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex items-center justify-between h-16 gap-3">

                <div class="flex items-center space-x-3 flex-shrink-0">
                    <img onclick="window.location.href='${isHomePage ? './' : '../'}'" src="${isHomePage ? 'image/favicon.png' : '../image/favicon.png'}" alt="EcoTrack Logo" class="w-10 h-10 object-contain" style="cursor: pointer; transform: scale(1.9);">
                </div>

                <div class="flex items-center gap-2 sm:gap-4 md:gap-6 ml-auto text-xs sm:text-sm whitespace-nowrap">
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
