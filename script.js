// --- 0. LẤY THÔNG TIN THÔNG TIN NGƯỜI DÙNG ĐĂNG NHẬP ---
const currentUser = JSON.parse(localStorage.getItem('user'));
const authToken = localStorage.getItem('auth_token');

// --- 1. KHỞI TẠO BẢN ĐỒ ---
const map = L.map('map').setView([19.8067, 105.7852], 13);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '© OpenStreetMap'
}).addTo(map);

let geojsonLayer = null;

// --- 2. BẢNG MÀU PHÂN LOẠI CÂY TRỒNG ---
function getColorByCrop(type) {
    if (!type) return '#9ca3af'; 
    const cropType = type.toString().trim().toUpperCase();

    if (cropType.includes('LÚA') || cropType === 'LUC') return '#10b981'; // Xanh lá
    if (cropType.includes('RAU') || cropType.includes('HOA MÀU') || cropType === 'BHK') return '#84cc16'; // Đọt chuối
    if (cropType.includes('QUẢ') || cropType.includes('CAM') || cropType.includes('XOÀI')) return '#f97316'; // Cam
    if (cropType.includes('CÀ PHÊ') || cropType === 'CLN') return '#8b5cf6'; // Tím
    if (cropType.includes('TÔM') || cropType.includes('CÁ') || cropType === 'NTS') return '#06b6d4'; // Xanh dương
    if (cropType.includes('Ở') || cropType === 'ONT') return '#ef4444'; // Đỏ

    return '#6b7280'; 
}

// --- 3. NẠP DỮ LIỆU BẢN ĐỒ DATA.JSON ---
async function loadGeoJSONToMap() {
    try {
        const response = await fetch('data.json');
        if (!response.ok) return;
        
        const geojsonData = await response.json();
        if (geojsonLayer) map.removeLayer(geojsonLayer);

        geojsonLayer = L.geoJSON(geojsonData, {
            style: function(feature) {
                const cropType = feature.properties ? 
                    (feature.properties.loai_nong_san || feature.properties.loai_dat) : null;
                const fillColor = getColorByCrop(cropType);

                return {
                    color: fillColor,
                    weight: 2,
                    fillColor: fillColor,
                    fillOpacity: 0.55
                };
            },
            onEachFeature: function(feature, layer) {
                if (feature.properties) {
                    let popupContent = `<div style="font-size: 13px;"><b>THÔNG TIN THỬA ĐẤT</b><hr style="margin: 4px 0;">`;
                    for (let key in feature.properties) {
                        popupContent += `<b>${key}:</b> ${feature.properties[key]}<br>`;
                    }
                    popupContent += `</div>`;
                    layer.bindPopup(popupContent);
                }
            }
        }).addTo(map);

        if (geojsonLayer.getBounds().isValid()) {
            map.fitBounds(geojsonLayer.getBounds());
        }
    } catch (error) {
        console.error("Lỗi nạp bản đồ:", error);
    }
}

// --- 4. QUẢN LÝ CHAT & LỊCH SỬ ---
let selectedBase64Image = null;
let currentSessionId = null;

function handleImageSelect(event) {
    const file = event.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = function(e) {
        selectedBase64Image = e.target.result;
        document.getElementById('image-preview').src = selectedBase64Image;
        document.getElementById('image-preview-container').style.display = 'flex';
    };
    reader.readAsDataURL(file);
}

function removeSelectedImage() {
    selectedBase64Image = null;
    const imgInput = document.getElementById('image-input');
    if (imgInput) imgInput.value = '';
    const previewContainer = document.getElementById('image-preview-container');
    if (previewContainer) previewContainer.style.display = 'none';
}

// Tự động nạp thông tin khi load trang
window.onload = function() {
    // Hiển thị tên người dùng đăng nhập
    const userDisplayEl = document.getElementById('displayUsername');
    if (userDisplayEl && currentUser) {
        userDisplayEl.innerText = currentUser.full_name || currentUser.username;
    }

    // Hiển thị nút trang Admin nếu người dùng có quyền Admin
    const adminBtnEl = document.getElementById('btnAdminPage');
    if (adminBtnEl && currentUser && currentUser.role === 'admin') {
        adminBtnEl.style.display = 'inline-flex';
    }

    loadGeoJSONToMap();
    renderHistorySidebar();
    startNewChat();
};

// Hàm xử lý Đăng xuất
function handleLogout() {
    if (confirm("Bạn có chắc chắn muốn đăng xuất khỏi hệ thống?")) {
        localStorage.removeItem('user');
        localStorage.removeItem('auth_token');
        window.location.href = '/login';
    }
}

function startNewChat() {
    currentSessionId = Date.now().toString();
    const chatBox = document.getElementById('chat-messages');
    chatBox.innerHTML = `
        <div class="message bot-message">Xin chào! Tôi có thể giúp bạn tra cứu thửa đất, thời tiết, giá nông sản hoặc phân tích ảnh cây trồng.</div>
    `;
    removeSelectedImage();
    renderHistorySidebar();
}

function getSessions() {
    return JSON.parse(localStorage.getItem('chat_sessions') || '{}');
}

function saveMessageToSession(role, text, imgBase64 = null) {
    let sessions = getSessions();
    if (!sessions[currentSessionId]) {
        sessions[currentSessionId] = {
            title: text.substring(0, 22) + (text.length > 22 ? "..." : ""),
            messages: []
        };
    }
    sessions[currentSessionId].messages.push({ role, text, imgBase64 });
    localStorage.setItem('chat_sessions', JSON.stringify(sessions));
    renderHistorySidebar();
}

function renderHistorySidebar() {
    const historyListEl = document.getElementById('history-list');
    if (!historyListEl) return;
    
    const sessions = getSessions();
    historyListEl.innerHTML = '';

    Object.keys(sessions).reverse().forEach(id => {
        const item = document.createElement('div');
        item.className = `history-item ${id === currentSessionId ? 'active' : ''}`;
        item.innerText = sessions[id].title || "Cuộc trò chuyện mới";
        item.onclick = () => loadSession(id);
        historyListEl.appendChild(item);
    });
}

function loadSession(sessionId) {
    currentSessionId = sessionId;
    const sessions = getSessions();
    const session = sessions[sessionId];
    if (!session) return;

    const chatBox = document.getElementById('chat-messages');
    chatBox.innerHTML = '';
    session.messages.forEach(msg => appendMessageUI(msg.role, msg.text, msg.imgBase64));
    renderHistorySidebar();
}

function handleKeyPress(event) {
    if (event.key === 'Enter') sendMessage();
}

// Gửi tin nhắn có kèm ID người dùng (user_id)
async function sendMessage() {
    const inputEl = document.getElementById('user-input');
    const text = inputEl.value.trim();
    if (!text && !selectedBase64Image) return;

    const currentImg = selectedBase64Image;
    inputEl.value = '';
    removeSelectedImage();

    appendMessageUI('user', text, currentImg);
    saveMessageToSession('user', text || "Đã gửi ảnh", currentImg);

    const loadingDiv = appendMessageUI('bot', 'Đang suy nghĩ...');

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${authToken}`
            },
            body: JSON.stringify({
                message: text || "Phân tích ảnh này giúp tôi.",
                user_id: currentUser ? currentUser.id : null, // Đã bổ sung truyền user_id
                image_base64: currentImg
            })
        });

        const data = await response.json();
        loadingDiv.innerText = data.reply;
        saveMessageToSession('bot', data.reply);
    } catch (error) {
        loadingDiv.innerText = "Lỗi kết nối máy chủ Backend.";
        saveMessageToSession('bot', "Lỗi kết nối máy chủ Backend.");
    }
}

function appendMessageUI(role, text, imgBase64 = null) {
    const chatBox = document.getElementById('chat-messages');
    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}-message`;

    if (imgBase64) {
        const img = document.createElement('img');
        img.src = imgBase64;
        img.className = 'chat-img-attached';
        msgDiv.appendChild(img);
    }

    const textNode = document.createElement('span');
    textNode.innerText = text;
    msgDiv.appendChild(textNode);

    chatBox.appendChild(msgDiv);
    chatBox.scrollTop = chatBox.scrollHeight;
    return textNode;
}
