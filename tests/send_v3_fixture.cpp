// Display fixtures only: no hardware and no claim to simulate ADC/Welch statistics.
#include <chrono>
#include <cmath>
#include <iostream>
#include <memory>
#include <thread>
#include "telemetry_sender.hpp"
int main(int argc, char** argv) {
    try {
        if (argc != 8) throw std::invalid_argument("ip s12_port s11_port s22_port phase_port coherence_port cw|burst");
        const std::string mode = argv[7];
        if (mode != "cw" && mode != "burst") throw std::invalid_argument("expected cw or burst");
        const auto session = TelemetrySender::new_session();
        std::array<std::unique_ptr<TelemetrySender>, 5> senders;
        for (unsigned t = 0; t < 5; ++t) {
            std::size_t used{};
            const auto port = std::stoul(argv[t+2], &used);
            if (used != std::strlen(argv[t+2]) || port == 0 || port > 65535)
                throw std::invalid_argument("port must be 1..65535");
            senders[t] = std::make_unique<TelemetrySender>(argv[1], static_cast<std::uint16_t>(port),
                session, static_cast<TelemetrySender::Payload>(t+1));
        }
        const auto origin = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
        std::vector<float> values(2048);
        for (unsigned f = 0; f < 8; ++f) {
            const unsigned selected = mode == "cw" ? 1126 : 1000+f*4;
            for (unsigned t = 0; t < 5; ++t) {
                for (unsigned bin = 0; bin < values.size(); ++bin) {
                    if (t < 3) values[bin] = bin == selected ? 70.f + t : 25.f;
                    else if (t == 3) values[bin] = bin == selected ? (mode == "cw" ? .5235987756f : -.8f+.05f*f)
                        : std::sin(float(bin*13+f));
                    else values[bin] = bin == selected ? .95f : .05f;
                }
                if (!senders[t]->send_spectrum(f, origin + f*199987, values).queued)
                    throw std::runtime_error("fixture send failed");
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
