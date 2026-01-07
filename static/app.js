const urlParams = new URLSearchParams(window.location.search);
const code = urlParams.get("code");
if (code) {
    document.getElementById("oauthCode").value = code;
    document.getElementById("status").innerText = "OAuth code received!";
}
