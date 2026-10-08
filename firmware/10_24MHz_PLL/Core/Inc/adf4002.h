#ifndef ADF4002_H
#define ADF4002_H

#include "main.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Configuration: 10 MHz reference, 24 MHz VCXO, RSET = 5.1 kohm. */
#define ADF4002_R_DIVIDER    5U
#define ADF4002_N_DIVIDER    12U

/* Provisional: 1 = positive, 0 = negative. Verify VCXO tuning direction. */
#define ADF4002_PD_POSITIVE  1U

/* 1 = digital lock detect, 2 = N divider output, 4 = R divider output. */
#define ADF4002_MUXOUT      1U

/* Current code 7 = nominal 5 mA with RSET = 5.1 kohm. */
#define ADF4002_CP_CODE     7U

/* Call once after MX_GPIO_Init() and MX_SPI2_Init(), with HAL tick running.
 * This driver uses SPI2 and ADF4002_LE_* definitions from main.h.
 * ADF4002 CE must be HIGH; this driver does not control CE.
 * HAL_OK confirms SPI transmission completed, not PLL lock/device receipt.
 */
HAL_StatusTypeDef ADF4002_Init(void);

#ifdef __cplusplus
}
#endif

#endif
