const urlParams = new URLSearchParams(window.location.search);
const code = document.getElementById("oauthCode").value;
if (code) {
    document.getElementById("status").innerText = "Signed in successfully!";
}

