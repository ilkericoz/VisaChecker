// Source: https://appointment.as-visa.com/PageJs/Macaristan/TR/istanbul/makeAppointment.js
// Fetched and saved for auto-book research
// Key findings:
//   - 40s bot trap: form must be open 40+ seconds before submit
//   - Submit endpoint: POST /tr/istanbul-bireysel-basvuru
//   - Requires cfToken (Cloudflare Turnstile) + recaptchaToken (reCAPTCHA v3)
//   - Field names come from inline JS vars: tcField, nameField, surnameField, emailField, passField

let pageLoadTime;
let isSubmitting = false;
let recaptchaReady = false;

function onTurnstileSuccess(token) {
    $('#cfToken').val(token);
}

async function generateRecaptchaToken(action = 'submit') {
    return new Promise((resolve) => {
        if (typeof grecaptcha === 'undefined') {
            resolve(null);
            return;
        }
        grecaptcha.ready(function () {
            recaptchaReady = true;
            grecaptcha.execute(window.recaptchaSiteKey, { action: action })
                .then(function (token) { resolve(token); })
                .catch(function (error) { resolve(null); });
        });
    });
}

$(document).ready(function () {
    pageLoadTime = Date.now();
    $('#formStartTime').val(pageLoadTime);

    if (window.recaptchaSiteKey) {
        const script = document.createElement('script');
        script.src = `https://www.google.com/recaptcha/api.js?render=${window.recaptchaSiteKey}`;
        script.async = true;
        script.defer = true;
        script.onload = () => { recaptchaReady = true; };
        document.head.appendChild(script);
    }

    $('#apForm').on('submit', function (e) {
        e.preventDefault();
        if (isSubmitting) return;
        isSubmitting = true;

        const cfToken = $('#cfToken').val();
        if (!cfToken) {
            isSubmitting = false;
            Swal.fire({ title: 'Doğrulama Eksik', text: 'Lütfen güvenlik doğrulamasını (Cloudflare) tamamlayınız.', icon: 'error' });
            return;
        }

        const $submitButton = $(this).find('[type="submit"]');
        $submitButton.prop('disabled', true).css('visibility', 'hidden');
        showSpinner();

        const kick = async () => {
            const recaptchaToken = await generateRecaptchaToken('appointment_submit');
            if (!recaptchaToken) {
                hideSpinner();
                $submitButton.prop('disabled', false).css('visibility', 'visible');
                isSubmitting = false;
                return;
            }
            $('#recaptchaToken').val(recaptchaToken);
            showWarning("Randevu başvurusu yapmak istediğinize emin misiniz?", false);
        };

        if (!recaptchaReady && typeof grecaptcha === 'undefined') {
            setTimeout(kick, 300);
        } else {
            kick();
        }
    });
});

function createRequest(lessThanFifteenDays) {
    const currentTime = Date.now();
    const elapsedTime = (currentTime - pageLoadTime) / 1000;

    // BOT TRAP: rejects if form submitted in under 40 seconds
    if (elapsedTime < 40) {
        hideSpinner();
        isSubmitting = false;
        Swal.fire({
            icon: 'warning',
            title: 'Şüpheli İşlem',
            text: 'İşleminiz çok hızlı yapıldığı için sistemimiz sizi bot olarak algıladı.',
            confirmButtonText: 'Tamam',
        }).then(() => { window.location.href = "https://www.google.com"; });
        return;
    }

    const fdata = new FormData();
    fdata.append('Nationality', $('select[name=Nationality]').val());
    fdata.append('Appointment', $('select[name=Appointment]').val());
    fdata.append('TravelDate', $('input[name=TravelDate]').val());
    fdata.append('TravelSubject', $('select[name=TravelSubject]').val());
    fdata.append('AppointmentDate', $('input[name=AppointmentDate]').val());
    fdata.append('AppointmentTime', $('select[name=AppointmentTime]').val());
    fdata.append(tcField, $('input[name="' + tcField + '"]').val());
    fdata.append('reTCKN', $('input[name=reTCKN]').val());
    fdata.append(passField, $('input[name="' + passField + '"]').val());
    fdata.append(nameField, $('input[name="' + nameField + '"]').val());
    fdata.append(surnameField, $('input[name="' + surnameField + '"]').val());
    fdata.append('FormNonce', $('input[name=FormNonce]').val());
    fdata.append('Phone', $('input[name=Phone]').val());
    fdata.append('CompanyName', $('input[name=CompanyName]').val());
    fdata.append(emailField, $('input[name="' + emailField + '"]').val());
    fdata.append('DogumYili', $('input[name=DogumYili]').val());
    fdata.append('rEmail', $('input[name=rEmail]').val());
    fdata.append('enteredCode', $('input[name=enteredCode]').val());
    fdata.append('__RequestVerificationToken', $('input[name=__RequestVerificationToken]').val());
    fdata.append('formStartTime', $('input[name=formStartTime]').val());
    fdata.append('cfToken', $('#cfToken').val());
    fdata.append('recaptchaToken', $('#recaptchaToken').val());
    fdata.append('lessThan15Days', lessThanFifteenDays);

    sendRequest(fdata);
}

function sendRequest(fdata) {
    $.ajax({
        url: '/tr/istanbul-bireysel-basvuru',
        processData: false,
        contentType: false,
        type: 'POST',
        data: fdata,
        timeout: 0,
        dataType: 'json',
        success: (response, textStatus, xhr) => {
            hideSpinner();
            // On success, redirects to confirmation URL from response
        },
        error: (xhr, status) => {
            hideSpinner();
            isSubmitting = false;
        }
    });
}
