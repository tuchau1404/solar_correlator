#include "adf4002.h"
#include "spi.h"

#if (ADF4002_R_DIVIDER < 1U) || (ADF4002_R_DIVIDER > 16383U)
#error "ADF4002 R divider must be between 1 and 16383"
#endif
#if (ADF4002_N_DIVIDER < 1U) || (ADF4002_N_DIVIDER > 8191U)
#error "ADF4002 N divider must be between 1 and 8191"
#endif
#if (ADF4002_PD_POSITIVE > 1U) || (ADF4002_MUXOUT > 7U) || (ADF4002_CP_CODE > 7U)
#error "Invalid ADF4002 configuration field"
#endif

static HAL_StatusTypeDef ADF4002_Write24(uint32_t word)
{
    uint8_t data[3] = {
        (uint8_t)(word >> 16),
        (uint8_t)(word >> 8),
        (uint8_t)word
    };

    HAL_GPIO_WritePin(ADF4002_LE_GPIO_Port,
                      ADF4002_LE_Pin, GPIO_PIN_RESET);

    HAL_StatusTypeDef status = HAL_SPI_Transmit(&hspi2, data, 3U, 100U);
    if (status != HAL_OK)
    {
        return status; /* Do not latch an incomplete transfer. */
    }

    HAL_Delay(1U);
    HAL_GPIO_WritePin(ADF4002_LE_GPIO_Port,
                      ADF4002_LE_Pin, GPIO_PIN_SET);
    HAL_Delay(1U);
    HAL_GPIO_WritePin(ADF4002_LE_GPIO_Port,
                      ADF4002_LE_Pin, GPIO_PIN_RESET);
    HAL_Delay(1U);
    return HAL_OK;
}

HAL_StatusTypeDef ADF4002_Init(void)
{
    const uint32_t fields =
        ((uint32_t)ADF4002_CP_CODE << 15) |
        ((uint32_t)ADF4002_CP_CODE << 18) |
        ((uint32_t)ADF4002_PD_POSITIVE << 7) |
        ((uint32_t)ADF4002_MUXOUT << 4);

    /* Reset, power-down, fastlock and CP three-state bits remain zero.
     * R latch: 2.9 ns antibacklash, 3-cycle digital lock detect precision.
     * N latch: CP gain zero selects current setting 1.
     */
    const uint32_t words[4] = {
        fields | 3UL,                           /* Initialization */
        fields | 2UL,                           /* Function */
        (uint32_t)ADF4002_R_DIVIDER << 2,         /* R */
        ((uint32_t)ADF4002_N_DIVIDER << 8) | 1UL /* N */
    };

    HAL_Delay(100U); /* Startup margin; supplies must already be stable. */
    for (uint32_t i = 0U; i < 4U; ++i)
    {
        HAL_StatusTypeDef status = ADF4002_Write24(words[i]);
        if (status != HAL_OK)
        {
            return status;
        }
    }
    return HAL_OK;
}
